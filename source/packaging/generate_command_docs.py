#!/usr/bin/python3
"""Generate installed, MCP-export and web-guide inputs from the shared contract."""
from pathlib import Path
import json
import sys

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE/'expanded'))
from framebridge_expanded.guide import export_guide, GUIDE_SCHEMA
from framebridge_expanded import __version__

def markdown(data, title):
    lines=['# '+title,'','Version: '+data['version']+' | Guide schema: '+str(GUIDE_SCHEMA),
           '','This reference is generated from the shared CLI/MCP guide. Use the matching installed version.',
           '','## Contents','']
    for i,(name,entry) in enumerate(data['topics'].items(),1):
        lines.append(str(i)+'. ['+entry['title']+'](#'+str(i)+'-'+name+')')
    for i,(name,entry) in enumerate(data['topics'].items(),1):
        lines.extend(['','<a id="'+str(i)+'-'+name+'"></a>','','## '+str(i)+'. '+entry['title'],''])
        for section in entry['sections']:
            lines.extend(['### '+section['heading'],'',section['body'],''])
            if section['commands']:
                lines.extend(['~~~sh',*section['commands'],'~~~',''])
    return '\n'.join(lines).rstrip()+'\n'

def roff(text):
    return text.replace('\\','\\e').replace('-','\\-')
def man(data):
    lines=['.TH FRAMEBRIDGE 1 "2026-10-27" "FrameBridge '+__version__+'" "User Commands"',
           '.SH NAME','framebridge, fb \\- use local Linux tools and enrolled nodes with ChatGPT',
           '.SH SYNOPSIS','.B framebridge',
           '[\\-\\-json] [\\-\\-state\\-dir DIRECTORY] COMMAND [ARGUMENTS]',
           '.SH DESCRIPTION',
           'FrameBridge provides one command-line interface for human operators and authorized agents.',
           'The names fb and framebridge execute the same program.',
           'The tools run on the selected Linux computers; the ChatGPT model remains hosted.']
    for entry in data['topics'].values():
        lines.extend(['.SH '+entry['title'].upper()])
        for section in entry['sections']:
            lines.extend(['.SS '+roff(section['heading']),roff(section['body'])])
            if section['commands']:
                lines.extend(['.PP','.nf',*(roff(x) for x in section['commands']),'.fi'])
    lines.extend(['.SH AUTHORS','Programming: ChatGPT.',
                  'Concept and project coordination: Gerardo Betancourt.',
                  '.SH LICENSE','GNU GPL version 3 only, with the applicable third-party license notices.',
                  'See the installed copyright and source license files.',
                  '.SH SEE ALSO','.BR systemctl (1), .BR dpkg (1), .BR apt (8)'])
    return '\n'.join(lines)+'\n'
def main():
    data=export_guide();doc=SOURCE/'docs';doc.mkdir(exist_ok=True)
    (doc/'COMMAND-GUIDE.json').write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n')
    ref=markdown(data,'FrameBridge — command guide')
    (doc/'COMMAND-REFERENCE.md').write_text(ref)
    (SOURCE.parent/'docs/COMMAND-REFERENCE.md').write_text(ref)
    (doc/'DEBIAN-TUTORIAL.md').write_text(markdown(data,'FrameBridge — Debian, Ubuntu and Linux Mint installation and first use'))
    (doc/'framebridge.1').write_text(man(data))
    print(json.dumps({'version':data['version'],'guide_schema':GUIDE_SCHEMA,'topics':len(data['topics']),'guide_sha256':data['guide_sha256'],'generated_files':5}))
if __name__=='__main__':main()
