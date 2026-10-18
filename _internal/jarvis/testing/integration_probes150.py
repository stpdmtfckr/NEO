"""Configured credentials are used locally for read-only checks, never exported.
No messages, playback, IMAP fetch, token refresh, or browser authorization here.
"""
from __future__ import annotations
import json
import time
from dataclasses import replace


def targets(config):
    from integration_test_runner import PROVIDERS
    found=[]
    for p in PROVIDERS:
        found.append((p.name,p,None))
        if p.name=='Discord':
            for index,profile in enumerate(config.get('integrations.discord.servers',[]) or []):
                if isinstance(profile,dict):found.append((f'Discord server {index+1}',p,index))
    found.append(('Mail.ru',None,None))
    return found


def check(target,config,session=None):
    from integration_test_runner import _credential,probe
    from jarvis.secrets import unprotect_secret
    items={name:(p,index) for name,p,index in targets(config)}
    if target not in items:return {'provider':target,'status':'NOT_CONFIGURED'}
    p,index=items[target]
    result={'provider':target,'scope':'read-only authentication; not messages/playback', 'request_budget':1}
    if p is None:
        if not config.get('integrations.mailru.enabled',False):return dict(result,status='DISABLED')
        password=_credential(config,'integrations.mailru.app_password_encrypted')
        email=config.get('integrations.mailru.email','')
        if not password or not email:return dict(result,status='NOT_CONFIGURED')
        try:
            import imaplib
            with imaplib.IMAP4_SSL('imap.mail.ru',993,timeout=10) as mail:
                mail.login(email,password)
                kind,_=mail.status('INBOX','(MESSAGES)')
            return dict(result,status='AUTHENTICATED' if kind=='OK' else 'UNAVAILABLE')
        except Exception as exc:
            return dict(result,status='UNAVAILABLE',error_type=type(exc).__name__)
    if not config.get(p.enabled_key,False):return dict(result,status='DISABLED')
    if index is None:credential=_credential(config,p.secret_key)
    else:
        profile=config.get('integrations.discord.servers',[])[index]
        if profile.get('enabled',True) is False:return dict(result,status='DISABLED')
        protected=profile.get('bot_token_encrypted') or profile.get('token_encrypted') or ''
        try:credential=str(unprotect_secret(protected) or '').strip() if protected else _credential(config,p.secret_key)
        except Exception:return dict(result,status='CREDENTIAL_UNAVAILABLE')
    if not credential:return dict(result,status='NOT_CONFIGURED')
    import requests
    own=session is None
    try:
        s=session or requests.Session()
        s.trust_env=True
        row=probe(p,credential,s)
        status=row.get('status','UNAVAILABLE');http=row.get('http_status')
        if http==401:status='AUTH_REJECTED_OR_EXPIRED'
        elif http==403:status='FORBIDDEN'
        elif http==429:status='RATE_LIMITED'
        elif status=='PASS':status='AUTHENTICATED' if p.credential_type in {'OAuth','Bearer','Bot','telegram'} else 'API_REQUEST_OK'
        hint=''
        if http==401:hint='The saved access token was rejected or expired. Reconnect this account in Settings; this read-only check does not refresh tokens.'
        elif http==403:hint='Check the permissions granted to this integration; do not replace unrelated keys.'
        elif http==429:hint='The provider rate limit was reached. Wait before retrying.'
        elif status=='UNAVAILABLE':hint='Connection was not confirmed. Inspect network access for this application; the token has not been proved invalid.'
        return {**result,**row,'status':status,**({'hint':hint} if hint else {})}
    except Exception as exc:return dict(result,status='UNAVAILABLE',error_type=type(exc).__name__)
    finally:
        credential=''
        if own and 's' in locals():s.close()


def main():
    import argparse
    from bootstrap import ReadOnlyConfig
    parser=argparse.ArgumentParser();parser.add_argument('--target',required=True);args=parser.parse_args()
    print(json.dumps(check(args.target,ReadOnlyConfig()),ensure_ascii=True))

if __name__=='__main__':main()
