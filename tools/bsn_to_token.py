import logging
import sys
import uuid
import json
import requests
import argparse

import nacl.hash
from nacl.encoding import URLSafeBase64Encoder

DEFAULT_SERVER_HOST = "https://tvs-connect.acc.coronacheck.nl"
DEFAULT_SERVER_PORT = 443

DEFAULT_CLIENT_ID = 'test_client'
DEFAULT_REDIRECT_URI = 'http://157.90.231.134:3000/login'

class BsnToTokenException(Exception):
    pass

def randstr():
    return uuid.uuid4().hex


def compute_code_challenge(code_verifier):
    verifier_hash = nacl.hash.sha256(code_verifier.encode('ISO_8859_1'), encoder=URLSafeBase64Encoder)
    code_challenge = verifier_hash.decode().replace('=', '')
    return code_challenge


def retrieve_token(base_url, redirect_uri, bsn):
    print(bsn)
    nonce = randstr()
    state = randstr()

    code_verifier = randstr()
    code_challenge = compute_code_challenge(code_verifier)

    params = {
        'client_id': args.client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': 'openid',
        'nonce': nonce,
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256'
    }

    auth_url = f'{base_url}/consume_bsn/{bsn}'

    code_state_resp = requests.get(auth_url, params=params, verify=False)
    if code_state_resp.status_code != 200:
        error_msg = json.loads(code_state_resp.text)['detail']
        raise BsnToTokenException(f"Invalid request, got status_code: {code_state_resp.status_code}. detail: {error_msg}")
    code_state_dict = json.loads(code_state_resp.text)

    code = code_state_dict['code'][0]
    state = code_state_dict['state'][0]
    data = f"client_id={args.client_id}&code={code}&state={state}&code_verifier={code_verifier}&" \
           f"grant_type=authorization_code&redirect_uri={redirect_uri}"
    at_url = f'{base_url}/accesstoken'

    accesstoken = requests.post(url=at_url, data=data)
    id_token = json.loads(accesstoken.text)['id_token']
    return id_token


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert a BSN to JWT Token tool.")
    parser.add_argument("--server-host", type=str, nargs='?', default=DEFAULT_SERVER_HOST,
                        help="Server host to request JWT token from")
    parser.add_argument("--server-port", type=int, nargs='?', default=DEFAULT_SERVER_PORT,
                        help="Server port to request JWT token from")
    parser.add_argument("--client-id", type=str, nargs='?', default=DEFAULT_CLIENT_ID,
                        help="Client ID to request JWT token from")
    parser.add_argument("--redirect-uri", type=str, nargs='?', default=DEFAULT_REDIRECT_URI,
                        help="Redirect uri belonging to the configured Client ID to request JWT token from")
    args = parser.parse_args()

    server_host: str = args.server_host
    server_port: int = args.server_port

    base_url = f'{server_host}:{server_port}'
    for inline in sys.stdin:
        try:
            bsn = inline.replace('\n', '')
            id_token = retrieve_token(base_url, args.redirect_uri, bsn)
            print(id_token)
        except BsnToTokenException as exception:
            logging.error(exception)
            exit()