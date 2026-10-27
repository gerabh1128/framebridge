"""Shared English guide content. Regenerate command docs after editing."""

from . import __version__

GUIDE = {'start': {'title': 'Start with FrameBridge',
           'sections': [{'heading': 'One interface for people and agents',
                         'body': 'FrameBridge provides tools and a shared command-line interface '
                                 'for human operators and authorized agents. fb is the short name '
                                 'for framebridge; both run the same program. The tools run on '
                                 'your Linux computers. The ChatGPT model remains hosted. Read '
                                 'this guide for the installed version before operating; confirm '
                                 'the receiver, target and available capabilities. Use the '
                                 'installed CLI and tools for normal work. Edit software only when '
                                 'the owner explicitly requests development, using a separate '
                                 'source clone and reviewable changes rather than rewriting the '
                                 'installed program.',
                         'commands': ['fb help', 'fb status', 'fb --json status']},
                        {'heading': 'Install and connect',
                         'body': 'Install the .deb built for your exact Debian 13, Ubuntu 24.04/26.04, Linux Mint 22.x or LMDE 7 amd64 profile. These compatibility candidates still require installation and lifecycle validation on each target. Select the artifact from the matching profile folder and keep the distribution system Python. Normal first connection does not require a separate setup, doctor or start sequence. Run connect in a normal interactive terminal; advanced setup is optional for central/node roles and public settings.',
                         'commands': ["sudo apt install ./framebridge-expanded_" + __version__ + "_amd64.deb", 'fb connect']},
                        {'heading': 'Updating and removal',
                         'body': 'Install an official update through the normal package manager when that update has been published. Normal removal stops package-owned managed processes and can retain the user profile and encrypted connection material; it is not a credential-forgetting operation. On systemd 255, package removal also revokes the administrative boot-credential grant. After reinstalling, run fb connect interactively to provision that grant again; retained files alone do not restore startup. Use the explicit credentials forget action first when that is your intended outcome. Do not assume purge erases user-profile data. An explicit downgrade also revokes administrative helper grants before the older package files replace the helper. Helper-backed profiles require a new interactive connect with a compatible package before managed startup can be restored.',
                         'commands': ['sudo dpkg --remove framebridge-original-expanded']},
                        {'heading': 'Human authentication',
                         'body': 'The normal connection asks for an optional account label, a '
                                 'hidden tunnel ID and a hidden MCP server access key. The label '
                                 'is metadata, not proof of ChatGPT login. Browser login/consent '
                                 'and normal operating-system authentication remain human actions '
                                 'in the normal interface. Never put credentials in chat, command '
                                 'arguments, documentation or example files. A successful managed '
                                 'connection returns to the prompt and can run independently of '
                                 'that terminal. Reboot only when an actual system change requires '
                                 'it.',
                         'commands': []},
                        {'heading': 'Choose the right guide',
                         'body': 'Use commands for the reference, connection and lifecycle for '
                                 'state changes, nodes for central/node work, terminals for '
                                 'persistent Bash, and screens for authentic CUA evidence. Query '
                                 'success alone does not mean the connection is ready. The guide '
                                 'describes registered operations; use status/doctor to '
                                 'investigate current state.',
                         'commands': ['fb help commands',
                                      'fb help connection',
                                      'fb help lifecycle']}]},
 'commands': {'title': 'Command reference',
              'sections': [{'heading': 'Everyday commands',
                            'body': 'connect establishes or resumes the managed ChatGPT '
                                    'connection. disconnect pauses that connection until an '
                                    'explicit connect, including across recovery and reboot. '
                                    'status reports runtime, connection, intended connection state '
                                    'and boot policy. doctor diagnoses prerequisites/problems. '
                                    'setup edits advanced stopped-profile settings. console opens '
                                    'the text work view. help reads this versioned reference.',
                            'commands': ['fb connect',
                                         'fb disconnect',
                                         'fb status',
                                         'fb status connection',
                                         'fb status runtime',
                                         'fb status boot',
                                         'fb status console',
                                         'fb doctor',
                                         'fb setup',
                                         'fb console',
                                         'fb help']},
                           {'heading': 'Services and boot',
                            'body': 'For an already configured managed profile, service start/stop '
                                    'control the current stack symmetrically. Boot enable/disable '
                                    'changes future startup policy without stopping current work '
                                    'or forgetting the saved credential. First node provisioning '
                                    'is an explicit exception described in lifecycle. Without '
                                    'managed setup, non-node service start starts local tools '
                                    'only; normal first connection should use connect. Read status '
                                    'before and after changes. The separate credentials forget '
                                    'action is destructive and requires explicit confirmation.',
                            'commands': ['fb service start',
                                         'fb service stop',
                                         'fb service boot enable',
                                         'fb service boot disable']},
                           {'heading': 'Console and specialized operations',
                            'body': 'console login manages optional work-view opening after an '
                                    'authenticated tty1 login; it does not enable automatic login. '
                                    'tty service manages the terminal daemon; tty list lists '
                                    'sessions. nodes, certificates and screen-access retain their '
                                    'domain-specific subcommands. Use --help at the relevant '
                                    'command for its exact arguments. Avoid compatibility aliases '
                                    'in new automation.',
                            'commands': ['fb console login status',
                                         'fb tty service status',
                                         'fb tty list',
                                         'fb nodes --help',
                                         'fb certificates --help',
                                         'fb screen-access --help']},
                           {'heading': 'Human and structured output',
                            'body': 'Use explicit commands in automation rather than the '
                                    'interactive no-argument menu. --json selects structured '
                                    'output for an agent; examine operation success, readiness and '
                                    'state separately. Read errors and the exit status. If human '
                                    'interaction is required, complete it in the normal '
                                    'terminal/interface rather than attempting to supply a secret '
                                    'through the agent. CLI JSON uses an envelope with '
                                    'schema_version, product, version, command, ok and result. '
                                    'Read runtime, connection and boot truth inside result; '
                                    'envelope ok denotes request success. Exit 0 means successful '
                                    'query/operation, not necessarily a connected transport; exit '
                                    '1 reports operation/diagnostic failure, exit 2 invalid '
                                    'arguments or required human interaction/confirmation, and '
                                    'exit 130 cancellation.',
                            'commands': ['framebridge --json status',
                                         'fb --json status connection']}]},
 'connection': {'title': 'Connect and disconnect ChatGPT',
                'sections': [{'heading': 'Normal managed connection',
                              'body': 'connect creates the default standalone profile when absent '
                                      'and establishes managed startup. It asks for initial human '
                                      'input in the terminal, checks progress, reports readiness '
                                      'or an actionable failure, and returns to the prompt. Valid '
                                      'saved setup can be resumed without repeating the initial '
                                      'fields. The MCP access key is not an OpenAI API key. Keep '
                                      "the browser's ChatGPT account login and consent separate "
                                      'from the optional local account label.',
                              'commands': ['fb connect', 'fb status connection']},
                             {'heading': 'Saved credentials and next boot',
                              'body': 'Managed startup uses protected, host-bound encrypted '
                                      'connection material. The normal terminal does not have to '
                                      'remain open. This is a persistence feature, not a promise '
                                      'that an account, network or tunnel service can never fail. '
                                      'Connection intent and boot policy are separate: enabled '
                                      'startup runs the stack at boot, while an intentional '
                                      'disconnected state remains disconnected until connect.',
                              'commands': ['fb status boot', 'fb status']},
                             {'heading': 'Intentional disconnect',
                              'body': 'disconnect keeps local tools and saved material, but '
                                      'records the intention to stay disconnected across worker '
                                      'recovery and reboot. Explicit connect clears that pause. '
                                      'Check the returned state rather than interpreting a '
                                      'successful query as an active transport.',
                              'commands': ['fb disconnect', 'fb status connection']},
                             {'heading': 'Replacement and temporary foreground mode',
                              'body': 'Use connect --replace only when deliberately replacing the '
                                      'account label/tunnel/access configuration. The advanced '
                                      '--foreground mode is temporary, needs a ready local runtime '
                                      'and occupies its terminal until interrupted; it is not the '
                                      'normal managed workflow. Do not use advanced mode merely to '
                                      'recover a normal saved connection.',
                              'commands': []}]},
 'lifecycle': {'title': 'Services, connection intent and boot policy',
               'sections': [{'heading': 'Separate states',
                             'body': 'FrameBridge distinguishes actual local runtime, desired '
                                     'connection, verified transport, boot enablement and saved '
                                     'credential presence. A healthy runtime can be intentionally '
                                     'disconnected; a service can be active while disabled for the '
                                     'next boot. Read the labelled status fields together.',
                             'commands': ['fb status', 'fb --json status']},
                            {'heading': 'Current managed stack',
                             'body': 'For an already configured managed profile, service start and '
                                     'service stop use the same current-stack boundary without '
                                     'changing boot enablement. Stop ends current managed work and '
                                     'transport but keeps profile, boot policy and saved material. '
                                     'Startup on the next boot follows the retained policy and '
                                     'intended connection state. A non-node profile without '
                                     'managed setup starts only local tools. Stopping a terminal '
                                     'service or session can interrupt its running work.',
                             'commands': ['fb service start', 'fb service stop']},
                            {'heading': 'Future boot policy',
                             'body': 'Once managed setup exists, service boot enable/disable '
                                     'changes unit enablement only. It does not immediately stop '
                                     'or start current work and does not delete saved connection '
                                     'material. Initial node provisioning is described separately '
                                     'below. Explicitly forgetting saved material is a separate '
                                     'destructive operation. Legacy autostart disable has its old '
                                     'broader destructive behavior; use the canonical service boot '
                                     'commands for new workflows.',
                             'commands': ['fb service boot enable',
                                          'fb service boot disable',
                                          'fb status boot']},
                            {'heading': 'First managed startup for a node',
                             'body': 'After advanced node setup and the required '
                                     'enrollment/approval, a node without a managed installation '
                                     'can run service start in a normal terminal. This first call '
                                     'creates managed startup, enables future boot and starts the '
                                     'node now; the result explicitly reports '
                                     'initial_configuration and boot_policy_changed. '
                                     'Alternatively, first service boot enable prepares and '
                                     'enables the node unit without starting current work. Initial '
                                     'preparation can require normal owner authentication and is '
                                     'not silently performed by an unattended JSON request. Once '
                                     'configured, the ordinary start/stop and boot-policy-only '
                                     'contracts apply. A node does not need its own ChatGPT '
                                     'tunnel.',
                             'commands': ['fb service start',
                                          'fb service boot enable',
                                          'fb status boot']},
                            {'heading': 'Console opening',
                             'body': 'Opening console is a view operation, separate from Linux '
                                     'service startup. Optional console login enable/disable '
                                     'changes the owned startup hook after an authenticated tty1 '
                                     'Bash login. This hook does not bypass normal login. Review '
                                     "the command's scope before changing it.",
                             'commands': ['fb console login status']}]},
 'nodes': {'title': 'Central and node operation',
           'sections': [{'heading': 'Roles',
                         'body': 'Standalone provides local tools. Central adds the broker for '
                                 'enrolled nodes. A node joins one selected central instance and '
                                 'uses its ChatGPT connection; it does not need another ChatGPT '
                                 'tunnel or phone pairing. Advanced setup selects the role. '
                                 'Preserve explicit authenticated node transport and real identity '
                                 'approval.',
                         'commands': ['fb setup', 'fb nodes --help']},
                        {'heading': 'Enrollment is not availability',
                         'body': 'Enrollment submission, central approval, node receipt of '
                                 'authorization and a running node agent are separate stages. '
                                 'status reports what it actually knows; an unchecked agent is not '
                                 'evidence that the node is online. Central list/pending/approve '
                                 'and node enroll/poll/status/run have role-specific purposes. '
                                 'poll can complete saved authorization; it is not a purely '
                                 'observational operation. After enrollment, consult the lifecycle '
                                 'topic for first managed node startup and its explicit initial '
                                 'boot-policy effect.',
                         'commands': ['fb nodes list', 'fb nodes pending', 'fb nodes status']},
                        {'heading': 'Targets',
                         'body': 'LOCAL means the actual receiving machine. Use enrolled, '
                                 'confirmed aliases or IDs and inspect the selected target before '
                                 'work. Do not guess SSH identities or use historical lab '
                                 'hostnames as defaults. CLI tty --node can use the authenticated '
                                 'node transport. The interactive console command is local; run it '
                                 'at the destination or use an explicitly configured SSH '
                                 'destination.',
                         'commands': []},
                        {'heading': 'Transport trust',
                         'body': 'certificates create manages a private central/node transport CA, '
                                 'not a public website certificate or ChatGPT account '
                                 'authentication. Private keys stay protected on the central host; '
                                 'distribute only the required public trust material. Off-loopback '
                                 'broker use requires correctly configured verified TLS. Do not '
                                 'recreate trust merely to work around a connection symptom.',
                         'commands': ['fb certificates --help']}]},
 'terminals': {'title': 'Persistent Bash terminals',
               'sections': [{'heading': 'Service and sessions',
                             'body': "tty service controls the profile's terminal daemon. tty open "
                                     'creates a real Bash session and returns its identifier. Use '
                                     'that identifier for subsequent observations and input. The '
                                     'same Bash preserves directory, variables and work between '
                                     'requests while the session/service remains alive. It does '
                                     'not preserve running processes across an operating-system '
                                     "reboot. Tool jobs use a profile-specific HOME; a job's home "
                                     'directory can therefore differ from the human login home. '
                                     "Commands retain the connecting account's normal "
                                     'operating-system permissions.',
                             'commands': ['fb tty service status', 'fb tty list', 'fb tty open']},
                            {'heading': 'Observe, type and keys',
                             'body': 'tty observe returns terminal transcript text, cursor, '
                                     'dimensions and process state; it is not an authentic '
                                     'full-screen image. tty type sends text without automatically '
                                     'adding Enter, though supplied newlines can affect execution. '
                                     'tty key accepts exact uppercase names such as ENTER and '
                                     'CTRL_C. Replace session arguments with an identifier '
                                     'returned by open/list; never guess one.',
                             'commands': ['fb tty key --help',
                                          'fb tty type --help',
                                          'fb tty observe --help']},
                            {'heading': 'Lifetime and closing',
                             'body': 'Default real idle expiry is disabled, but a profile can '
                                     'configure a timeout. Global status/list does not refresh '
                                     "each session's per-session activity. close destroys a "
                                     'session rather than merely separating a viewer. Graceful '
                                     'service stop attempts to close its sessions; elevated or '
                                     'unverifiable process state can prevent cleanup. Inspect the '
                                     'outcome instead of assuming that all child processes '
                                     'disappeared.',
                             'commands': ['fb tty close --help', 'fb tty resize --help']},
                            {'heading': 'Sudo mode',
                             'body': "A session's sudo on/off controls normal privilege "
                                     'eligibility; it does not log in as root or bypass '
                                     'operating-system authentication. Changing the mode recreates '
                                     'Bash, preserving directory and dimensions but not shell '
                                     'variables or internal state. Authentication stays in the '
                                     'normal human terminal/interface. Never send a password '
                                     'through type or chat.',
                             'commands': ['fb tty sudo --help']},
                            {'heading': 'Work console panels',
                             'body': 'console combines an ordinary Bash pane with a read-only '
                                     'FBTERM activity dashboard. The dashboard is not a second '
                                     'interactive shell. The tmux prefix is Ctrl+B: release both '
                                     'keys, then press lowercase z to enlarge/restore a pane, or '
                                     'lowercase o to switch panes. The release accepts common '
                                     'zoom-key variants within its dedicated console. Closing a '
                                     'viewer normally leaves its tmux session alive; leaving Bash, '
                                     'killing its server or rebooting is different. In a desktop '
                                     'terminal the shared work view is horizontal with FBTERM on '
                                     'the left and Bash on the right. Explicit TTY mode is '
                                     'vertical with a short FBTERM monitor above Bash. The layout '
                                     'belongs to the shared tmux window and follows the last '
                                     'requested view mode; it is not isolated per viewer.',
                             'commands': ['fb console', 'fb status console']}]},
 'screens': {'title': 'Authentic screen observation and input',
             'sections': [{'heading': 'Text transcripts and pixels',
                           'body': 'A CLI tty transcript describes a terminal and explicitly does '
                                   'not claim authentic pixels. MCP real-screen CUA-TTY and '
                                   'graphical screen tools operate through the real screen client '
                                   'and capture-linked interaction. Use those supported '
                                   'observation tools when authentic complete screen evidence is '
                                   'required. Never present a reconstructed terminal, animation or '
                                   'partial invented view as a real screenshot.',
                           'commands': []},
                          {'heading': 'Automatic graphical discovery',
                           'body': 'The installer and control console stay text interfaces in both TTY and a desktop terminal. Compatible graphical CUA is discovered automatically; availability depends on the actual display/session and permissions. A loaded module is not proof that real pixel capture or input succeeded. New profiles use adaptive auto mode: without a verified active X11 session the runtime provides FBEXEC, FBTERM and CUA-TTY in text mode, without requiring a graphical login or automatically creating a private desktop. Explicit virtual-desktop settings remain advanced options and are labelled separately from physical-screen evidence. Physical-screen capture and input currently require the supported active X11 session. Native Wayland control is not validated; showing an XWayland window does not establish full-desktop capture or input.',
                           'commands': ['fb status', 'fb doctor']},
                          {'heading': 'Frame-buffer read access',
                           'body': 'screen-access is a narrow, explicit administrative permission '
                                   'for reading the real frame buffer. It does not by itself '
                                   'enable graphical CUA, keyboard/mouse control or arbitrary VT '
                                   'switching. Persistent read access can remain effective after '
                                   "logout or a VT switch; review the command's displayed scope "
                                   'and use normal local administrator authentication.',
                           'commands': ['fb screen-access status']},
                          {'heading': 'Shared desktop',
                           'body': 'A graphical desktop shares focus, keyboard and mouse. Use one '
                                   'controlling agent per desktop at a time and avoid overlapping '
                                   'work. This is an operating recommendation, not a claim of '
                                   'certified automatic isolation. Independent non-overlapping '
                                   'execution jobs may be possible; do not infer isolation or '
                                   'physical parallel execution from that alone.',
                           'commands': []}]},
 'diagnostics': {'title': 'Status and diagnosis',
                 'sections': [{'heading': 'Inspect before acting',
                               'body': 'status summarizes current runtime, connection, desired '
                                       'state and boot policy. A query completing successfully is '
                                       'distinct from the components being ready. Connection '
                                       'verification has a freshness boundary; retrying or unknown '
                                       'must not be treated as connected. Focused status uses the '
                                       'same underlying state model.',
                               'commands': ['fb status',
                                            'fb status connection',
                                            'fb status runtime',
                                            'fb status boot']},
                              {'heading': 'Doctor',
                               'body': 'doctor diagnoses prerequisites and reports findings with suggested next steps. It is not an implicit repair, and it is not a substitute for checking actual live connection state. Inspect the diagnosis and relevant private application logs before considering configuration changes. Candidate eligibility checks the exact OS release, amd64 architecture, package profile and CPython ABI before starting native components. Debian 13/LMDE 7 use cp313, Ubuntu 24.04/Mint 22.x use cp312, and Ubuntu 26.04 uses cp314. A matching profile does not establish a tested installation; never repair an ABI mismatch by replacing system Python or weakening package dependencies.',
                               'commands': ['fb doctor']},
                              {'heading': 'Logs and progress',
                               'body': 'Normal connection presents concise progress and a final '
                                       'result, then returns to the prompt. Detailed diagnostics '
                                       'belong in protected application logs. Share only the '
                                       'minimum relevant redacted result; never expose '
                                       'credentials, private keys, browser session data or '
                                       'unrelated personal paths. Avoid rerunning setup, '
                                       'enrollment or broad lifecycle tests merely because a view '
                                       'is confusing.',
                               'commands': ['fb help connection', 'fb help lifecycle']}]},
 'security': {'title': 'Authorization, distribution and safe operation',
              'sections': [{'heading': 'Authorized scope',
                            'body': 'An agent can use the documented CLI and MCP tools within the '
                                    "user's authorized scope. Reading this guide does not grant "
                                    'permission. Verify the receiver and selected target. Normal '
                                    'human terminal/browser authentication remains separate; do '
                                    'not request credentials in chat or invent phone/Owner-TTY '
                                    'enrollment requirements. Normal use should operate through '
                                    'the installed CLI and tools, not edit or rebuild installed '
                                    'FrameBridge program files. If the owner explicitly requests '
                                    'development, use a separate source clone, make reviewable '
                                    'changes, and do not present a modified build as an official '
                                    'release. This operating guidance does not restrict GNU GPL '
                                    'freedoms or require private forks to submit changes upstream.',
                            'commands': []},
                           {'heading': 'Official releases',
                            'body': 'The source is distributed under GNU GPLv3-only terms and the '
                                    'applicable third-party notices. Verify official release '
                                    'checksums and published signatures/repository metadata before '
                                    'installing. Signature verification identifies the publisher '
                                    'and detects alteration; it does not prohibit lawful modified '
                                    'versions. Official releases are reviewed through the '
                                    "project's release process.",
                            'commands': []},
                           {'heading': 'Side effects',
                            'body': 'Read status before service stop, credential forgetting, node '
                                    'approval, startup-hook changes or administrative screen '
                                    'permission changes. Explain material scope and consequences '
                                    "and follow the user's authorization. A sensitive operation "
                                    'must report the actual result, not just that a query '
                                    'succeeded. Keep real captures complete and distinguish '
                                    'observations from hypotheses.',
                            'commands': []}]},
 'compatibility': {'title': 'Compatibility with earlier commands',
                   'sections': [{'heading': 'Preferred spelling',
                                 'body': 'Use the short canonical interface in new documentation '
                                         'and automation. fb and framebridge remain equivalent. '
                                         'Older connect chatgpt, disconnect chatgpt and connection '
                                         'status spellings are compatibility routes. New status '
                                         'scopes replace the need to learn an extra connection '
                                         'noun.',
                                 'commands': ['fb connect',
                                              'fb disconnect',
                                              'fb status connection']},
                                {'heading': 'Legacy lifecycle behavior',
                                 'body': 'Older root start/stop and autostart commands are '
                                         'retained for compatibility where documented, but their '
                                         'historical scopes can differ from the canonical service '
                                         'controls. In particular, legacy autostart disable '
                                         'removes managed setup and saved encrypted material; it '
                                         'is not an alias for boot-policy-only disable. Read the '
                                         "command's help before using a legacy script.",
                                 'commands': ['fb help lifecycle']},
                                {'heading': 'Versioned help',
                                 'body': 'This guide describes the installed product and guide '
                                         'schema. Read the matching release notes for any semantic '
                                         'migration. The CLI help, MCP guide and static web '
                                         'documentation are generated from the same reviewed guide '
                                         'data; a cached manual from an earlier build can describe '
                                         'a different workflow.',
                                 'commands': ['fb help compatibility']}]}}
