f = open(r'd:\Downloads\Projects\My Coding Agent\lean-agent\ui\index.html', encoding='utf-8').read()
tests = [
    ('switchSidebar', 'function switchSidebar(tab, el)'),
    ('historyPanel toggle', "historyPanel').style.display"),
    ('renderMsg', 'function renderMsg(role, content'),
    ('renderHistory', 'function renderHistory(sessions)'),
    ('decideAll', 'function decideAll(v)'),
    ('reviewActionBar HTML', 'id="reviewActionBar"'),
    ('thought-row CSS', '.thought-row {'),
    ('chat-text CSS', '.chat-text {'),
]
artifact = 'non// ====='
print('ARTIFACT PRESENT:', artifact in f)
for name, needle in tests:
    print(('OK  ' if needle in f else 'MISS'), name)
print('Total lines:', f.count('\n'))
