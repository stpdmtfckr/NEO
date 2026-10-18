"""Validate native grammar identity and publish a reproducible corpus comparison.

Maintainer command: python export_phrase_expansion.py --baseline /path/to/NEO144
No registry handler, operating-system action or external API is called.
"""
import argparse
import json
from pathlib import Path
import re
from jarvis.runtime.canonical_commands import templates, parse_canonical_command, _bind, _SLOT_NAMES
from jarvis.runtime.natural_commands import LANGUAGES


def export(baseline):
    root=Path(__file__).resolve().parent.parent
    catalogue=json.loads((root/'_internal/tests/fixtures/command_catalogue.json').read_text(encoding='utf-8'))
    schemas={row['tool']:row['schema'] for row in catalogue['tools']}
    languages={};failures=[];slot_mismatches=[];duplicates=[];forms_verified=0
    for code in LANGUAGES:
        before=json.loads((baseline/'_internal/jarvis/locales/commands'/f'{code}.json').read_text())['action_phrases']
        after=json.loads((root/'_internal/jarvis/locales/commands'/f'{code}.json').read_text())
        growth={k:{'before':len(before.get(k,[])),'after':len(v),'ratio':round(len(v)/len(before[k]),4) if before.get(k) else None} for k,v in after['action_phrases'].items()}
        native=after['native_variants'];executable=clarifications=0
        for action,phrases in native.items():
            slots=set(re.findall(r'\{(\w+)\}',phrases[0]))
            if len(set(p.casefold() for p in phrases))!=len(phrases):duplicates.append([code,action])
            for phrase in phrases:
                if set(re.findall(r'\{(\w+)\}',phrase))!=slots:slot_mismatches.append([code,action,phrase])
        rows=templates(code)
        if len(rows)!=sum(map(len,native.values())):failures.append([code,'template_count','Unsafe or duplicate authored templates were skipped'])
        for row in rows:
            properties=schemas[row.intent.split('.')[0]]['properties']
            def fill(match):
                key=_SLOT_NAMES.get(row.intent,{}).get(match[1],match[1]);schema=properties.get(key,{})
                if schema.get('type') in {'number','integer'}:return str(max(2,schema.get('minimum',2)))
                if schema.get('enum'):return schema['enum'][0]
                return 'Ctrl+S' if key=='keys' else 'Example'
            command=re.sub(r'\{(\w+)\}',fill,row.phrase)
            parsed=parse_canonical_command(command,code,schemas.__getitem__);forms_verified+=1
            if row.intent in {'automation.queue','automation.parallel'}:
                if not parsed or parsed.error!='invalid_slots':failures.append([code,row.intent,'Expected clarification'])
                clarifications+=1;continue
            expected=_bind(row,row.pattern.fullmatch(command).groupdict(),schemas[row.intent.split('.')[0]],command,code)
            if not parsed or parsed.error or (parsed.tool,parsed.arguments)!=expected:failures.append([code,row.intent,command,str(parsed),expected])
            else:executable+=1
        languages[code]={'actions':len(native),'native_forms_before':len(native),'native_forms_after':sum(map(len,native.values())),
            'native_per_action_before':1,'native_per_action_after_min':min(map(len,native.values())),'native_per_action_after_max':max(map(len,native.values())),
            'full_atlas_before':sum(map(len,before.values())),'full_atlas_after':sum(map(len,after['action_phrases'].values())),
            'full_atlas_min_growth':min(v['ratio'] for v in growth.values() if v['ratio'] is not None),'full_atlas_max_growth':max(v['ratio'] for v in growth.values() if v['ratio'] is not None),
            'executable_typed_forms_verified':executable,'clarification_forms_verified':clarifications,'per_action':growth}
    report={'scope':'Pure selected-language grammar and strict registry contract; no OS or external API effects. Native paraphrases counted separately from legacy forms and reusable task frames.',
        'canonical_action_ids':430,'enumerated_registry_actions':catalogue['enumerated_action_count'],
        'difference':'translate.translate uses a translation operation without an action enum. True action aliases remain separate declared IDs.',
        'forms_verified':forms_verified,'cross_action_mismatches':failures,'duplicate_forms':duplicates,'slot_mismatches':slot_mismatches,'languages':languages,
        'clarification_only_actions':['automation.queue','automation.parallel'],
        'clarification_reason':'The utterance alone has no concrete task list; requests for details are intentional and no work is fabricated.'}
    target=root/'docs/PHRASE_EXPANSION_145.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    assert not failures and not duplicates and not slot_mismatches,(failures,duplicates,slot_mismatches)
    print(json.dumps({'forms_verified':forms_verified,'executable_forms':sum(v['executable_typed_forms_verified'] for v in languages.values()),'clarifications':sum(v['clarification_forms_verified'] for v in languages.values()),'failures':0,'output':str(target)}))
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--baseline',type=Path,required=True)
    export(parser.parse_args().baseline)
