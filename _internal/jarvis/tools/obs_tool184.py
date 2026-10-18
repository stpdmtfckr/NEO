"""OBS adapter. Stream authority is a trusted one-use scope, never tool data."""
from contextlib import contextmanager,nullcontext
from contextvars import ContextVar
from copy import deepcopy
from jarvis.runtime.streaming_commands184 import ACTIONS,LANGUAGES,parse_obs_command,obs_input_scope
from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition,ToolExecution

_GRANT=ContextVar('obs_live_grant184',default=None)


def safe_detail(arguments,execution):
    """Optional AutoText developer labels receive no scene/path/provider data."""
    action=arguments.get('action')
    return 'obs_control.'+(action if action in ACTIONS else 'unknown')+': '+('success' if execution.success else 'failed')


@contextmanager
def confirmed_stream_start(name,arguments,claim_text='',expected_identity=None):
    code=next((c for c in LANGUAGES if parse_obs_command(claim_text,c)==(name,arguments)),None)
    grant=(deepcopy(arguments),expected_identity) if name=='obs_control' and arguments.get('action')=='stream_start' and code is not None else None
    token=_GRANT.set(grant)
    try:
        with obs_input_scope(claim_text,code) if code is not None else nullcontext():yield
    finally:_GRANT.reset(token)


def register(registry,config):
    def run(arguments):
        grant=_GRANT.get();_GRANT.set(None)
        confirmed=grant is not None and grant[0]==arguments
        from jarvis.integrations.obs_service184 import shared
        result=shared(config).execute(arguments,confirmed_stream=confirmed,expected_identity=grant[1] if confirmed else None)
        return ToolExecution(bool(result.get('ok')),str(result.get('message') or ''),result,
            outcome='success' if result.get('ok') else 'blocked' if result.get('status')=='confirmation_required' else 'user_state')
    registry.register(ToolDefinition('obs_control',
      'Explicit OBS Studio controls. Requires naming OBS. Live stream always requires confirmation. Platform titles, chat and moderation are separate integrations.',
      {'type':'object','properties':{
          'action':{'type':'string','enum':list(ACTIONS)},'scene':{'type':'string','maxLength':512},
          'input':{'type':'string','maxLength':512},'source':{'type':'string','maxLength':512},
          'volume':{'type':'number','minimum':0,'maximum':100}},'required':['action'],'additionalProperties':False},
      run,lambda args:Risk.HIGH if args.get('action')=='stream_start' else Risk.SAFE))
