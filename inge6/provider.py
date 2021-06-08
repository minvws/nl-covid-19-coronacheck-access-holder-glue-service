import base64
import json
import logging
import requests

from urllib.parse import parse_qs, urlencode
from typing import Optional

from fastapi import Request, Response, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.encoders import jsonable_encoder

from oic.oic.message import TokenErrorResponse
from pyop.exceptions import (
    InvalidAuthenticationRequest,
    InvalidClientAuthentication, OAuthError
)

from .config import settings
from .cache import redis_cache
from .utils import create_post_autosubmit_form
from .encrypt import Encrypt
from .models import AuthorizeRequest

from .saml.exceptions import UserNotAuthenticated
from .saml.provider import Provider as SAMLProvider
from .saml import (
    AuthNRequest, ArtifactResolveRequest, ArtifactResponseParser
)

from .oidc.provider import Provider as OIDCProvider
from .oidc.authorize import (
    is_authorized,
    accesstoken
)


_PROVIDER = None

def get_provider(app = None):
    global _PROVIDER # pylint: disable=global-statement
    if _PROVIDER is None:
        if app is None:
            raise Exception("app cannot be None on first call.")
        _PROVIDER = Provider(app)
    return _PROVIDER

class Provider(OIDCProvider, SAMLProvider):
    I6_PRIV_KEY = settings.bsn.i6_priv_key
    I4_PUB_KEY = settings.bsn.i4_pub_key
    SYMM_KEY = settings.bsn.symm_key

    def __init__(self, app, **kwargs):
        super().__init__(app, **kwargs)

        self.bsn_encrypt = Encrypt(
            i6_priv=self.I6_PRIV_KEY,
            i4_pub=self.I4_PUB_KEY,
            local_enc_key=self.SYMM_KEY
        )

    def _create_authn_post_context(self, relay_state, url):
        saml_request = AuthNRequest()
        return {
            'sso_url': url,
            'saml_request': saml_request.get_base64_string().decode(),
            'relay_state': relay_state
        }

    def _cache_auth_req(self, randstate, auth_req, authorization_request):
        value = {
            'auth_req': auth_req,
            'code_challenge': authorization_request.code_challenge,
            'code_challenge_method': authorization_request.code_challenge_method
        }
        redis_cache.hset(randstate, 'auth_req', value)

    def _create_redis_bsn_key(self, id_token):
        return

    def authorize_endpoint(self, authorize_request: AuthorizeRequest, headers):
        try:
            auth_req = self.parse_authentication_request(urlencode(authorize_request.dict()), headers)
        except InvalidAuthenticationRequest as invalid_auth_req:
            logging.getLogger().debug('received invalid authn request', exc_info=True)
            error_url = invalid_auth_req.to_error_url()
            if error_url:
                return RedirectResponse(error_url, status_code=303)

            return Response(content='Something went wrong: {}'.format(str(invalid_auth_req)), status_code=400)

        randstate = redis_cache.gen_token()
        self._cache_auth_req(randstate, auth_req, authorize_request)
        return HTMLResponse(content=self._login(randstate))

    def token_endpoint(self, body, headers):
        code = parse_qs(body.decode())['code'][0]
        artifact = redis_cache.hget(code, 'arti')

        try:
            token_response = accesstoken(self, body, headers)
            encrypted_bsn = self._resolve_artifact(artifact)

            access_key = self._create_redis_bsn_key(token_response['id_token'].encode())
            redis_cache.set(access_key, encrypted_bsn)

            json_content_resp = jsonable_encoder(token_response.to_dict())
            return JSONResponse(content=json_content_resp)
        except UserNotAuthenticated as user_not_authenticated:
            logging.getLogger().debug('invalid client authentication at token endpoint', exc_info=True)
            error_resp = {
                'error': user_not_authenticated.oauth_error,
                'error_description': str(user_not_authenticated)
            }
            response = JSONResponse(jsonable_encoder(error_resp), status_code=400)
            response.headers['WWW-Authenticate'] = 'Basic'
            return response
        except InvalidClientAuthentication as invalid_client_auth:
            logging.getLogger().debug('invalid client authentication at token endpoint', exc_info=True)
            error_resp = TokenErrorResponse(error='invalid_client', error_description=str(invalid_client_auth))
            response = Response(error_resp.to_json(), status_code=401)
            response.headers['Content-Type'] = 'application/json'
            response.headers['WWW-Authenticate'] = 'Basic'
            return response
        except OAuthError as oauth_error:
            logging.getLogger().debug('invalid request: %s', str(oauth_error), exc_info=True)
            error_resp = TokenErrorResponse(error=oauth_error.oauth_error, error_description=str(oauth_error))
            response = Response(error_resp.to_json(), status_code=400)
            response.headers['Content-Type'] = 'application/json'
            return response

    def _login(self, randstate: str, force_digid: Optional[bool] = False):
        if settings.mock_digid.lower() == "true" and not force_digid:
            authn_post_ctx = self._create_authn_post_context(relay_state=randstate, url=f'/digid-mock?state={randstate}')
        else:
            authn_post_ctx = self._create_authn_post_context(relay_state=randstate, url=...)

        return create_post_autosubmit_form(authn_post_ctx)

    def assertion_consumer_service(self, request: Request):
        state = request.query_params['RelayState']
        artifact = request.query_params['SAMLart']

        if 'mocking' in request.query_params:
            redis_cache.set('DIGID_MOCK' + artifact, 'true')

        auth_req_dict = redis_cache.hget(state, 'auth_req')
        auth_req = auth_req_dict['auth_req']

        authn_response = self.authorize(auth_req, 'test_client')
        response_url = authn_response.request(auth_req['redirect_uri'], False)
        code = authn_response['code']

        redis_cache.hset(code, 'arti', artifact)
        self._store_code_challenge(code, auth_req_dict['code_challenge'], auth_req_dict['code_challenge_method'])
        return RedirectResponse(response_url, status_code=303)

    def disable_access_token(self, b64_id_token):
        redis_cache.delete('', b64_id_token.decode())

    def _store_code_challenge(self, code, code_challenge, code_challenge_method):
        value = {
            'code_challenge': code_challenge,
            'code_challenge_method': code_challenge_method
        }
        redis_cache.hset(code, 'cc_cm', value)

    def _resolve_artifact(self, artifact) -> bytes:
        is_digid_mock = redis_cache.get('DIGID_MOCK' + artifact)
        if settings.mock_digid.lower() == "true" and is_digid_mock is not None:
            return self.bsn_encrypt.symm_encrypt(artifact)

        resolve_artifact_req = ArtifactResolveRequest(artifact).get_xml()
        url = self.idp_metadata.get_artifact_rs()['location']
        headers = {
            'SOAPAction' : '"https://artifact-pp2.toegang.overheid.nl/kvs/rd/resolve_artifact"',
            'content-type': 'text/xml'
        }
        resolved_artifact = requests.post(url, headers=headers, data=resolve_artifact_req, cert=('saml/certs/sp.crt', 'saml/certs/sp.key'))
        artifact_response = ArtifactResponseParser(resolved_artifact.text, self.idp_metadata, verify=False)
        artifact_response.raise_for_status()

        bsn = artifact_response.get_bsn()
        encrypted_bsn = self.bsn_encrypt.symm_encrypt(bsn)
        return encrypted_bsn

    def bsn_attribute(self, request: Request):
        id_token: str = is_authorized(request)

        redis_bsn_key = self._create_redis_bsn_key(id_token)
        attributes = redis_cache.get(redis_bsn_key.decode())
        self.disable_access_token(redis_bsn_key)

        if attributes is None:
            raise HTTPException(status_code=408, detail="Resource expired.Try again after /authorize", )

        decoded_json = base64.b64decode(attributes).decode()
        bsn_dict = json.loads(decoded_json)
        encrypted_bsn = self.bsn_encrypt.from_symm_to_pub(bsn_dict)
        return Response(content=encrypted_bsn, status_code=200)

    def metadata(self):
        errors = self.sp_metadata.validate()

        if len(errors) == 0:
            return Response(content=self.sp_metadata.get_xml().decode(), media_type="application/xml")

        raise HTTPException(status_code=500, detail=', '.join(errors))
