"""Explicit PowerShell script syntax, preserving literal script for risk review."""
import re
from jarvis.runtime.natural_commands import LANGUAGES,strip_request_shell,negated_action
HEADS=(
 ('выполни скрипт PowerShell','выполни следующий скрипт PowerShell'),
 ('run PowerShell script','run the following PowerShell script'),
 ('führe das PowerShell-Skript aus','führe das folgende PowerShell-Skript aus'),
 ('exécute le script PowerShell','exécute le script PowerShell suivant'),
 ('ejecuta el script PowerShell','ejecuta el siguiente script PowerShell'),
 ('esegui lo script PowerShell','esegui il seguente script PowerShell'),
 ('executa o script PowerShell','executa o seguinte script PowerShell'),
)
def script_request157(text,code):
    if code not in LANGUAGES or negated_action(text,code):return None
    value=strip_request_shell(text,code)
    for head in HEADS[LANGUAGES.index(code)]:
        match=re.fullmatch(re.escape(head)+r'\s*:\s*(?P<script>.+)',value,re.I|re.S)
        if match:return 'powershell',{'script':match['script']}
    return None
