
let ws, monacoEditor, currentWS, activeThought = null;
let pendingImages = [];
let openFiles = [];
let activeFilePath = null;
let terminals = [];
let activeTerminalId = null;
let termCounter = 0;
let thinkStartTime = null;
let currentZoom = 13;
let mdPreviewActive = false;
let imgScale = 1;
let imgPos = { x: 0, y: 0 };
let imgDragging = false;
let imgStart = { x: 0, y: 0 };

try {
    if (typeof marked !== 'undefined') {
        marked.setOptions({
            highlight: function(code, lang) {
                if (lang && typeof hljs !== 'undefined' && hljs.getLanguage(lang)) {
                    try { return hljs.highlight(code, { language: lang }).value; } catch(e) {}
                }
                return typeof hljs !== 'undefined' ? hljs.highlightAuto(code).value : code;
            },
            breaks: true,
            gfm: true,
        });
    }
} catch(e) { console.warn("Marked setup failed:", e); }

try {
    if (typeof require !== 'undefined') {
        require.config({ paths: { vs: 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.44.0/min/vs' } });
        require(['vs/editor/editor.main'], function() {
            monacoEditor = monaco.editor.create(document.getElementById('monacoContainer'), {
                value: '',
                language: 'javascript',
                theme: 'vs-dark',
                automaticLayout: true,
                padding: { top: 14 },
                lineNumbers: 'on',
                mouseWheelZoom: true,
                scrollBeyondLastLine: false,
                wordWrap: "on"
            });
            monacoEditor.onDidChangeModelContent(() => {
                if (activeFilePath && activeFilePath.endsWith('.md')) {
                    updateMdPreview();
                }
                if (activeFilePath) {
                    const tab = openFiles.find(f => f.path === activeFilePath);
                    if (tab) tab.content = monacoEditor.getValue();
                }
            });
        });
    }
} catch(e) { console.warn("Monaco setup failed:", e); }

function renderMarkdown(text) {
    if (typeof marked === 'undefined') return text || '';
    
    // Pre-process: protect LaTeX from marked's escaping
    const mathBlocks = [];
    let processed = (text || '')
        .replace(/\$\$([\s\S]+?)\$\$/g, (_, math) => {
            mathBlocks.push({ type: 'block', math });
            return `<MATHBLOCK${mathBlocks.length - 1}>`;
        })
        .replace(/(?<!\w)\$([^\$\n]+?)\$(?!\w)/g, (_, math) => {
            mathBlocks.push({ type: 'inline', math });
            return `<MATHINLINE${mathBlocks.length - 1}>`;
        });
    
    let html = marked.parse(processed);
    
    if (typeof katex !== 'undefined') {
        html = html.replace(/<MATHBLOCK(\d+)>/g, (_, i) => {
            try { return katex.renderToString(mathBlocks[i].math, { displayMode: true, throwOnError: false }); } catch(e) { return mathBlocks[i].math; }
        }).replace(/<MATHINLINE(\d+)>/g, (_, i) => {
            try { return katex.renderToString(mathBlocks[i].math, { displayMode: false, throwOnError: false }); } catch(e) { return '$' + mathBlocks[i].math + '$'; }
        });
    }
    
    return html;
}

function adjustZoom(delta) {
    if (delta === 0) currentZoom = 13;
    else currentZoom = Math.max(8, Math.min(40, currentZoom + delta));
    monacoEditor.updateOptions({ fontSize: currentZoom });
    document.getElementById('editorStatus').textContent = `Zoom: ${Math.round(currentZoom/13*100)}%`;
}

function toggleMdPreview() {
    mdPreviewActive = !mdPreviewActive;
    const p = document.getElementById('markdownPreview');
    const b = document.getElementById('mdPreviewBtn');
    p.style.display = mdPreviewActive ? 'block' : 'none';
    b.classList.toggle('active', mdPreviewActive);
    if (mdPreviewActive) updateMdPreview();
    monacoEditor.layout();
}

function updateMdPreview() {
    if (!activeFilePath || !activeFilePath.endsWith('.md')) return;
    const content = monacoEditor.getValue();
    const p = document.getElementById('markdownPreview');
    p.innerHTML = renderMarkdown(content);
}

// IMAGE PAN/ZOOM LOGIC
function handleImgWheel(e) {
    if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        const delta = e.deltaY > 0 ? 0.9 : 1.1;
        imgScale = Math.max(0.1, Math.min(10, imgScale * delta));
        updateImgTransform();
    }
}

function handleImgDown(e) {
    if (e.button !== 0) return;
    imgDragging = true;
    imgStart = { x: e.clientX - imgPos.x, y: e.clientY - imgStart.y };
    document.addEventListener('mousemove', handleImgMove);
    document.addEventListener('mouseup', handleImgUp);
}

function handleImgMove(e) {
    if (!imgDragging) return;
    imgPos.x = e.clientX - imgStart.x;
    imgPos.y = e.clientY - imgStart.y;
    updateImgTransform();
}

function handleImgUp() {
    imgDragging = false;
    document.removeEventListener('mousemove', handleImgMove);
    document.removeEventListener('mouseup', handleImgUp);
}

function updateImgTransform() {
    const img = document.getElementById('imageViewerImg');
    img.style.transform = `translate(${imgPos.x}px, ${imgPos.y}px) scale(${imgScale})`;
    document.getElementById('imgZoomDisplay').textContent = `${Math.round(imgScale * 100)}%`;
}

function resetImgView() {
    imgScale = 1;
    imgPos = { x: 0, y: 0 };
    updateImgTransform();
}

// Start core app systems immediately!
connect();
addNewTerminal();


function detectLanguage(p) {
    if (!p) return 'plaintext';
    const e = p.split('.').pop().toLowerCase();
    return {py:'python',js:'javascript',ts:'typescript',html:'html',css:'css',json:'json',md:'markdown',sh:'shell',bat:'bat',ps1:'powershell',yml:'yaml',yaml:'yaml',rs:'rust',go:'go',cpp:'cpp',c:'c',java:'java'}[e] || 'plaintext';
}

function getFileIcon(name) {
    const e = name.split('.').pop().toLowerCase();
    return {py:'🐍',js:'📜',ts:'📘',html:'🌐',css:'🎨',json:'⚙️',md:'📝',sh:'⚡',bat:'⚡',ps1:'⚡',yml:'⚙️',yaml:'⚙️',txt:'📄',png:'🖼',jpg:'🖼',jpeg:'🖼',gif:'🖼',svg:'🖼'}[e] || '📄';
}

// ===== FILE TABS =====
function openFileInEditor(path, content) {
    const name = path.split('/').pop().split('\\').pop();
    const existing = openFiles.find(f => f.path === path);
    if (!existing) {
        openFiles.push({ path, name, content, language: detectLanguage(path) });
    } else {
        existing.content = content; // Refresh content
    }
    setActiveTab(path);
    renderTabs();
}

function setActiveTab(path) {
    activeFilePath = path;
    const tab = openFiles.find(f => f.path === path);
    if (!tab) return;
    
    // Reset Toolbar States
    document.getElementById('mdTools').style.display = path.endsWith('.md') ? 'flex' : 'none';
    const isImg = tab.content && tab.content.startsWith('[Image:');
    document.getElementById('editorToolbar').style.display = isImg ? 'none' : 'flex';

    if (isImg) {
        const parts = tab.content.split('|');
        const name = parts[0].replace('[Image: ', '').replace(']', '');
        const src = parts[1] || '';
        
        const iv = document.getElementById('imageViewer');
        const img = document.getElementById('imageViewerImg');
        const label = document.getElementById('imageViewerLabel');
        
        img.src = src;
        label.textContent = name;
        iv.style.display = 'flex';
        document.getElementById('imageViewer').style.zIndex = 150;
        document.querySelector('.editor-split-container').style.display = 'none';
        resetImgView();
    } else {
        document.getElementById('imageViewer').style.display = 'none';
        document.querySelector('.editor-split-container').style.display = 'flex';
        if (monacoEditor) {
            const lang = detectLanguage(path);
            monaco.editor.setModelLanguage(monacoEditor.getModel(), lang);
            monacoEditor.setValue(tab.content || '');
            if (path.endsWith('.md') && mdPreviewActive) updateMdPreview();
            else document.getElementById('markdownPreview').style.display = 'none';
        }
    }
    renderTabs();
}

function closeImageViewer() {
    document.getElementById('imageViewer').style.display = 'none';
    document.getElementById('monacoContainer').style.display = 'block';
}

function closeTab(path, e) {
    if (e) e.stopPropagation();
    openFiles = openFiles.filter(f => f.path !== path);
    if (activeFilePath === path) {
        activeFilePath = openFiles.length ? openFiles[openFiles.length - 1].path : null;
        if (activeFilePath) setActiveTab(activeFilePath);
        else if (monacoEditor) monacoEditor.setValue('');
    }
    renderTabs();
}

function renderTabs() {
    const bar = document.getElementById('tabsBar');
    if (!openFiles.length) {
        bar.innerHTML = '<div style="padding:0 12px;color:var(--text-dim);font-size:11px;display:flex;align-items:center;height:100%">No files open</div>';
        return;
    }
    bar.innerHTML = openFiles.map(f => `
        <div class="file-tab ${f.path === activeFilePath ? 'active' : ''}" onclick="setActiveTab('${f.path.replace(/\\/g,'\\\\')}')" title="${f.path}">
            <span class="tab-icon">${getFileIcon(f.name)}</span>
            <span class="tab-name">${f.name}</span>
            <span class="tab-close" onclick="closeTab('${f.path.replace(/\\/g,'\\\\')}',event)">✕</span>
        </div>`).join('');
}

// ===== MULTI-TERMINAL =====
function addNewTerminal() {
    termCounter++;
    const id = 'term_' + termCounter;
    const name = `Shell ${termCounter}`;
    terminals.push({ id, name, history: [] });

    const body = document.createElement('div');
    body.className = 'terminal-body';
    body.id = id;
    document.getElementById('termBodies').appendChild(body);

    switchTerminal(id);
    renderTermTabs();
}

function switchTerminal(id) {
    activeTerminalId = id;
    document.querySelectorAll('.terminal-body').forEach(el =>
        el.classList.toggle('active', el.id === id)
    );
    renderTermTabs();
}

function closeTerminal(id, e) {
    if (e) e.stopPropagation();
    if (terminals.length === 1) return; // Keep at least one
    terminals = terminals.filter(t => t.id !== id);
    const el = document.getElementById(id);
    if (el) el.remove();
    if (activeTerminalId === id) switchTerminal(terminals[terminals.length - 1].id);
    renderTermTabs();
}

function renderTermTabs() {
    const bar = document.getElementById('termTabsBar');
    // Keep the + button
    const newBtn = bar.querySelector('.new-term-btn');
    bar.innerHTML = '';
    terminals.forEach(t => {
        const tab = document.createElement('div');
        tab.className = 'term-tab' + (t.id === activeTerminalId ? ' active' : '');
        tab.innerHTML = `<span>🖥 ${t.name}</span><span class="term-tab-close" onclick="closeTerminal('${t.id}',event)" title="Close">✕</span>`;
        tab.onclick = () => switchTerminal(t.id);
        bar.appendChild(tab);
    });
    if (newBtn) bar.appendChild(newBtn);
    else {
        const nb = document.createElement('button');
        nb.className = 'new-term-btn'; nb.title = 'New Terminal'; nb.textContent = '＋';
        nb.onclick = addNewTerminal;
        bar.appendChild(nb);
    }
}

function logToActiveTerminal(html) {
    const el = document.getElementById(activeTerminalId);
    if (!el) return;
    el.insertAdjacentHTML('beforeend', `<div>${html}</div>`);
    el.scrollTop = 99999;
}

// ===== WEBSOCKET =====
function updateStatus(state) {
    const chip = document.getElementById('connStatus');
    const txt = document.getElementById('statusText');
    chip.className = 'status-chip status-' + state;
    txt.innerText = state.charAt(0).toUpperCase() + state.slice(1);
    if (state === 'offline') txt.innerText = 'Disconnected';
}

function connect() {
    updateStatus('connecting');
    ws = new WebSocket('ws://localhost:8001/ws');
    ws.onopen = () => {
        updateStatus('online');
    };
    ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
    ws.onclose = () => {
        updateStatus('offline');
        setTimeout(connect, 3000);
    };
    ws.onerror = () => {
        updateStatus('offline');
    };
}

function handleEvent(data) {
    if (data.type === "file_tree") renderTree(data.tree);
    else if (data.type === "workspace_update") {
        currentWS = data.path;
        document.getElementById('wsPath').innerText = data.path;
        document.getElementById('termPromptDisplay').innerText = `PS ${data.path} >`;
    }
    else if (data.type === "history_list") renderHistory(data.sessions);
    else if (data.type === "session_loaded") {
        document.getElementById('chat').innerHTML = '';
        data.messages.forEach(m => {
            if (m.role==='system') return;
            let content = m.content, imgs = [];
            if (Array.isArray(content)) {
                let txt = '';
                content.forEach(c => { if(c.type==='text') txt=c.text; if(c.type==='image_url') imgs.push(c.image_url.url); });
                content = txt;
            }
            renderMsg(m.role==='user'?'user':'assistant', content, imgs);
        });
    }
    else if (data.type === "file_content") {
        const path = data.path;
        const name = path.split('/').pop().split('\\').pop();
        const ext = name.split('.').pop().toLowerCase();
        const imageExts = ['png','jpg','jpeg','gif','webp','svg','bmp','ico'];
        document.querySelectorAll('.tree-item').forEach(el => el.classList.remove('active'));
        
        if (imageExts.includes(ext)) {
            // Show image in the image viewer panel
            const iv = document.getElementById('imageViewer');
            const img = document.getElementById('imageViewerImg');
            const label = document.getElementById('imageViewerLabel');
            // Request backend to serve base64 – but we just try as a file:// URL first
            // The bridge returns base64 in content for binary files, or we use the path
            const b64 = data.result.content;
            const src = b64 ? `data:image/${ext};base64,${b64}` : '';
            img.src = src;
            label.textContent = name;
            iv.style.display = 'flex';
            document.getElementById('monacoContainer').style.display = 'none';
            // Add a tab entry - store the image source in content
            openFileInEditor(path, `[Image: ${name}]|${src}`);
        } else {
            const content = data.result.content || data.result.error || '';
            document.getElementById('imageViewer').style.display = 'none';
            document.getElementById('monacoContainer').style.display = 'block';
            openFileInEditor(path, content);
        }
    }
    else if (data.type === "direct_terminal_result") {
        if (data.agent_controlled) {
            let agentTerm = terminals.find(t => t.id === 'term_agent');
            if (!agentTerm) {
                terminals.push({ id: 'term_agent', name: '🤖 Agent Shell', history: [] });
                const body = document.createElement('div');
                body.className = 'terminal-body';
                body.id = 'term_agent';
                document.getElementById('termBodies').appendChild(body);
                switchTerminal('term_agent');
            } else if (activeTerminalId !== 'term_agent') {
                const tabs = document.querySelectorAll('.sidebar-tab');
                if (tabs.length > 0) tabs[0].click();
                switchTerminal('term_agent');
            }
            const el = document.getElementById('term_agent');
            if (data.html) {
                el.insertAdjacentHTML('beforeend', data.html);
            } else {
                let out = (data.stdout||'').replace(/</g, '&lt;').replace(/\\n/g, '<br>');
                let err = (data.stderr||'').replace(/</g, '&lt;').replace(/\\n/g, '<br>');
                el.insertAdjacentHTML('beforeend', `<span>${out}</span><span style="color:var(--danger)">${err}</span>`);
            }
            el.scrollTop = 99999;
        } else {
            logToActiveTerminal((data.stdout||'') + `<span style="color:var(--danger)">${data.stderr||''}</span>`);
        }
    }
    else if (data.type === "agent_thinking") {
        const row = document.createElement('div');
        row.className = 'think-row chat-entry';
        row.id = 'last-think-row'; // Tag the newest one
        const elapsed = thinkStartTime ? Math.round((Date.now() - thinkStartTime) / 1000) : 0;
        thinkStartTime = Date.now(); // Reset for next thought
        const label = elapsed > 0 ? `Thought for ${elapsed}s` : 'Reasoning';
        row.innerHTML = `
            <div class="think-header" style="display:flex;align-items:center;">
                <button class="think-toggle" onclick="this.classList.toggle('open'); this.parentElement.nextElementSibling.classList.toggle('visible')">
                    <span class="t-arrow">▶</span>
                    <span>${label} &rsaquo;</span>
                </button>
            </div>
            <div class="think-body">${(data.content || '').replace(/</g,'&lt;')}</div>
        `;
        document.getElementById('chat').appendChild(row);
        document.getElementById('chat').scrollTop = 99999;
    }
    else if (data.type === "tool_start") activeThought = addThought(data.tool);
    else if (data.type === "require_approval") {
        renderReviewCard(data);
        // Add "Waiting" indicator to the last reasoning block
        const lastThink = document.getElementById('last-think-row');
        if (lastThink) {
            const btn = lastThink.querySelector('.think-toggle');
            if (btn && !btn.querySelector('.pend-tag')) {
                btn.classList.add('pending');
                btn.insertAdjacentHTML('beforeend', `<span class="pend-tag">Waiting for input</span>`);
            }
        }
    }
    else if (data.type === "tool_result") {
        if (activeThought) {
            finalizeThought(activeThought, data.result, data.modified_files);
            activeThought = null;
        } else if (pendingReviewCard) {
            const isErr = data.result && (data.result.error || (data.result.code !== undefined && data.result.code !== 0));
            const icon = isErr ? '✖' : '✓';
            const color = isErr ? '#f85149' : '#5a8f7b';
            const label = isErr ? 'Failed.' : 'Changes applied.';
            const div = pendingReviewCard.querySelector('div');
            if (div) div.innerHTML = `<span style="color:${color}">${icon}</span> <span style="color:#8a8a9a">${label}</span>`;
            pendingReviewCard = null;
        }
    }
    else if (data.type === "final_response") {
        activeThought = null;
        setAgentRunning(false);
        renderMsg('assistant', data.content);
    }
    else if (data.type === "agent_stopped") {
        activeThought = null;
        setAgentRunning(false);
    }
    else if (data.type === "session_saved") {
        const ws_name = data.workspace ? data.workspace.split('\\').pop().split('/').pop() : 'workspace';
        showToast(`💾 Saved: ${ws_name}`);
    }
}

// ===== VISION =====
document.getElementById('fileInput').onchange = (e) => {
    Array.from(e.target.files).forEach(f => { const r=new FileReader(); r.onload=ev=>addPreview(ev.target.result); r.readAsDataURL(f); });
    e.target.value='';
};
document.getElementById('prompt').addEventListener('paste', (e) => {
    const items = (e.clipboardData||e.originalEvent.clipboardData).items;
    for (const it of items) if (it.type.indexOf('image')!==-1) { const r=new FileReader(); r.onload=ev=>addPreview(ev.target.result); r.readAsDataURL(it.getAsFile()); }
});
function addPreview(b64) {
    pendingImages.push(b64);
    const c=document.getElementById('imagePreviews');
    const d=document.createElement('div'); d.className='img-preview-bubble';
    const i=pendingImages.length-1;
    d.innerHTML=`<img src="${b64}"><div class="img-preview-remove" onclick="rmPreview(${i},this.parentElement)">×</div>`;
    c.appendChild(d);
}
function rmPreview(i,el){ pendingImages.splice(i,1); el.remove(); }

// ===== TREE =====
function collapseAllFolders() {
    document.querySelectorAll('.tree-children').forEach(el => el.classList.add('collapsed'));
    document.querySelectorAll('.folder-arrow').forEach(el => el.textContent = '▶');
}
function refreshTree() { ws.send(JSON.stringify({type:'refresh_tree'})); }

function renderTree(node) {
    const container = document.getElementById('fileTree');
    const frag = document.createDocumentFragment();
    function walk(n, parent, depth) {
        if (n.type === 'folder') {
            const wrapper = document.createElement('div');
            const header = document.createElement('div'); header.className='tree-item tree-folder';
            header.style.paddingLeft = (10 + depth*14) + 'px';
            header.innerHTML = `<span class="folder-arrow" style="font-size:9px;transition:.15s">▶</span><span>📂</span><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${n.name}</span>`;
            const children = document.createElement('div'); children.className='tree-children collapsed';
            header.onclick = (e) => {
                e.stopPropagation();
                const collapsed = children.classList.toggle('collapsed');
                header.querySelector('.folder-arrow').textContent = collapsed ? '▶' : '▼';
                if (!collapsed) children.style.maxHeight = '9999px';
            };
            wrapper.appendChild(header); wrapper.appendChild(children);
            parent.appendChild(wrapper);
            if (n.children) n.children.forEach(c => walk(c, children, depth+1));
        } else {
            const el = document.createElement('div'); el.className='tree-item';
            el.style.paddingLeft = (10 + depth*14) + 'px';
            el.dataset.path = n.path;
            const name = n.name;
            const ext = name.split('.').pop().toLowerCase();
            const imageExts = ['png','jpg','jpeg','gif','webp','svg','bmp','ico'];
            el.innerHTML = `<span>${getFileIcon(name)}</span><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${name}</span>`;
            
            // Single click = select (highlight only)
            el.onclick = (e) => {
                e.stopPropagation();
                document.querySelectorAll('.tree-item').forEach(x => x.classList.remove('active'));
                el.classList.add('active');
            };
            // Double click = open file in editor/viewer
            el.ondblclick = (e) => {
                e.stopPropagation();
                document.querySelectorAll('.tree-item').forEach(x => x.classList.remove('active'));
                el.classList.add('active');
                // Normalize path to use forward slashes for consistency
                const normalizedPath = n.path.replace(/\\/g, '/');
                ws.send(JSON.stringify({type:'get_file', path: normalizedPath}));
            };
            parent.appendChild(el);
        }
    }
    walk(node, frag, 0);
    container.innerHTML = '';
    container.appendChild(frag);
}

// ===== SIDEBAR =====
function switchSidebar(tab, el) {
    document.querySelectorAll('.sidebar-tab').forEach(t=>t.classList.remove('active')); el.classList.add('active');
    document.getElementById('explorerPanel').style.display = tab==='explorer' ? 'flex' : 'none';
    document.getElementById('historyPanel').style.display = tab==='history' ? 'block' : 'none';
}

// ===== CHAT =====
let pendingReviews = []; // Track pending review cards

function renderMsg(role, content, images=[]) {
    const entry = document.createElement('div');
    entry.className = 'chat-entry';
    const isUser = role === 'user';
    let imgHTML = images.map(s => `<img src="${s}">`).join('');
    const renderedContent = isUser
        ? `<span style="white-space:pre-wrap">${(content||'').replace(/</g,'&lt;')}</span>`
        : renderMarkdown(content || '');
    entry.innerHTML = `
        <div class="chat-role-label ${role}">${isUser ? '👤 You' : '⚛️ AntiGravity'}</div>
        <div class="chat-text ${role} md-body">${imgHTML}${renderedContent}</div>
    `;
    if (!isUser) {
        const copyBtn = document.createElement('button');
        copyBtn.className = 'copy-btn';
        copyBtn.textContent = '⌘ Copy';
        copyBtn.title = 'Copy response text';
        copyBtn.onclick = () => {
            navigator.clipboard.writeText(content || '').then(() => {
                copyBtn.textContent = '✓ Copied!';
                setTimeout(() => { copyBtn.textContent = '⌘ Copy'; }, 1800);
            });
        };
        entry.appendChild(copyBtn);
    }
    document.getElementById('chat').appendChild(entry);
    document.getElementById('chat').scrollTop = 99999;
}

let thoughtTimers = {};

function addThought(tool) {
    const startTime = Date.now();
    const row = document.createElement('div'); row.className = 'thought-row'; row.dataset.tool = tool;

    const toggleBtn = document.createElement('button'); toggleBtn.className = 'thought-toggle';
    toggleBtn.innerHTML = `<span class="toggle-arrow">▶</span><span class="pulse-icon" style="color:#8a8a9a">⚡</span><span class="thought-label">Running ${tool}...</span>`;
    
    const body = document.createElement('div'); body.className = 'thought-body';
    body.textContent = 'Initializing...';
    
    toggleBtn.onclick = () => {
        toggleBtn.classList.toggle('open');
        body.classList.toggle('visible');
    };
    row.appendChild(toggleBtn); row.appendChild(body);
    document.getElementById('chat').appendChild(row);
    document.getElementById('chat').scrollTop = 99999;
    
    thoughtTimers[tool + '_start'] = startTime;
    return row;
}

function finalizeThought(row, res, ml=[]) {
    const tn = row.dataset.tool || 'Op';
    const isErr = res.error || (res.code !== undefined && res.code !== 0);
    const elapsed = Math.round((Date.now() - (thoughtTimers[tn + '_start'] || Date.now())) / 1000);
    const label = elapsed > 0 ? `${elapsed}s` : '<1s';

    const toggle = row.querySelector('.thought-toggle');
    const body = row.querySelector('.thought-body');
    const statusIcon = isErr ? '✖' : '✓';
    const statusColor = isErr ? '#f85149' : '#5a8f7b';
    toggle.innerHTML = `<span class="toggle-arrow">▶</span><span style="color:${statusColor}">${statusIcon}</span><span style="color:#8a8a9a">Worked for ${label} ›</span>`;
    body.textContent = res.stdout || res.stderr || res.error || (res.status === 'success' ? 'Done.' : 'Completed.');

    // File diff badges
    if (ml && ml.length > 0) {
        const diffBar = document.createElement('div'); diffBar.className = 'file-diff-bar';
        ml.forEach(f => {
            const fname = f.split('/').pop().split('\\').pop();
            const fpath = '...' + f.slice(-30);
            const item = document.createElement('div'); item.className = 'file-diff-item';
            item.innerHTML = `<span class="file-diff-icon">${getFileIconForName(fname)}</span><span class="file-diff-name">${fname}</span><span class="diff-adds">+?</span><span class="file-diff-path">${fpath}</span>`;
            item.onclick = () => ws.send(JSON.stringify({type:'get_file', path: f.replace(/\\/g,'/')}));
            diffBar.appendChild(item);
        });
        row.appendChild(diffBar);
    }
}

function getFileIconForName(name) {
    const e = name.split('.').pop().toLowerCase();
    return {py:'🐍',js:'📜',ts:'📘',html:'🌐',css:'🎨',json:'⚙️',md:'📝',sh:'⚡',bat:'⚡',ps1:'⚡',yml:'⚙️'}[e] || '📄';
}

// Active review tracking
let activeReviewCard = null;
let pendingReviewCard = null;

function renderReviewCard(data) {
    const fname = data.args && data.args.path ? data.args.path.split('/').pop().split('\\').pop() : data.tool;
    const fpath = data.args && data.args.path ? '...' + data.args.path.slice(-35) : '';

    // Show sticky bar
    document.getElementById('reviewBarLabel').textContent = `${fname} · waiting for review`;
    document.getElementById('reviewActionBar').style.display = 'flex';

    // Render diff inside a thought-row style card
    const row = document.createElement('div'); row.className = 'thought-row chat-entry'; row.id = 'activeReviewRow';
    row.style.borderLeft = '3px solid #f59e0b';
    
    let diff = data.diff
        ? data.diff.split('\n').map(l =>
            `<span style="color:${l.startsWith('+')?'#3fb950':l.startsWith('-')?'#f85149':'#555568'}">${l.replace(/</g,'&lt;')}\n</span>`
          ).join('')
        : (data.args && data.args.command ? `<span style="color:#e5c07b;white-space:pre-wrap;">${data.args.command.replace(/</g,'&lt;')}</span>` : 'No diff available.');

    // File badge header
    const header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0 8px;font-size:12px;';
    header.innerHTML = `<span style="color:#f59e0b">🛠</span><span class="file-diff-name" style="color:#c9d1d9">${fname}</span><span class="file-diff-path">${fpath}</span>`;
    
    const diffBox = document.createElement('div');
    diffBox.style.cssText = 'background:#111113;border:1px solid #2a2a2e;border-radius:8px;padding:10px 12px;font-family:\'JetBrains Mono\';font-size:10px;max-height:220px;overflow-y:auto;white-space:pre;';
    diffBox.innerHTML = diff;
    
    const btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex;gap:8px;margin-top:10px;';
    btnRow.innerHTML = `<button class="btn-accept-all" style="flex:1" onclick="decide(true,this)">✓ Accept</button><button class="btn-reject-all" style="border:1px solid #3a3a42;flex:1" onclick="decide(false,this)">✕ Reject</button>`;

    row.appendChild(header); row.appendChild(diffBox); row.appendChild(btnRow);
    document.getElementById('chat').appendChild(row);
    document.getElementById('chat').scrollTop = 99999;
    activeReviewCard = row;
    
    // Hide global loader while waiting for user
    const loader = document.getElementById('globalLoader');
    if (loader) loader.style.display = 'none';
}

function decide(v, b) {
    const row = b.closest('.thought-row');
    const icon = v ? '⚡' : '✗';
    const color = v ? '#3fb950' : '#f85149';
    const label = v ? 'Applying changes...' : 'Rejected';
    row.querySelector('div').innerHTML = `<span style="color:${color}" class="${v?'pulse-icon':''}">${icon}</span> <span style="color:#8a8a9a">${label}</span>`;
    row.querySelectorAll('div:not(:first-child)').forEach(e => e.remove());
    document.getElementById('reviewActionBar').style.display = 'none';
    ws.send(JSON.stringify({type:'approval_decision', decision: v}));
    
    // Clear waiting tags
    const lastThink = document.getElementById('last-think-row');
    if (lastThink) {
        const btn = lastThink.querySelector('.think-toggle');
        if (btn) {
            btn.classList.remove('pending');
            const tag = btn.querySelector('.pend-tag');
            if (tag) tag.remove();
        }
        lastThink.id = ''; // Unmark it
    }
    
    if (v) pendingReviewCard = row;
    activeReviewCard = null;
    
    // Restore global loader if agent is still running
    if (agentRunning) {
        const loader = document.getElementById('globalLoader');
        if (loader) {
            loader.style.display = 'block';
            document.getElementById('chat').appendChild(loader);
            document.getElementById('chat').scrollTop = 99999;
        }
    }
}

function decideAll(v) {
    if (activeReviewCard) {
        const btn = activeReviewCard.querySelector(v ? '.btn-accept-all' : '.btn-reject-all');
        if (btn) decide(v, btn);
    }
}
function timeAgo(dateString) {
    if (!dateString) return '';
    const diff = Math.floor((new Date() - new Date(dateString)) / 1000);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

function renderHistory(sessions) {
    const h = document.getElementById('historyPanel'); h.innerHTML = '';
    if (!sessions.length) { h.innerHTML = '<div style="text-align:center;padding:30px;color:var(--text-dim);font-size:13px">No saved sessions</div>'; return; }
    
    const frag = document.createDocumentFragment();
    sessions.forEach(s => {
        const title = s.title || s.workspace.split('\\').pop().split('/').pop() || s.workspace;
        let urlBadge = '';
        if (s.deepseek_url) {
            urlBadge = `<div class="h-link-badge" onclick="window.open('${s.deepseek_url}', '_blank'); event.stopPropagation();" title="${s.deepseek_url}">
                <span>🔗</span><span class="h-url-text">${s.deepseek_url.replace('https://','').replace('chat.deepseek.com/a/chat/s/','')}</span>
            </div>`;
        }

        const d = document.createElement('div'); d.className = 'history-item';
        d.innerHTML = `
            <div class="h-header" onclick="loadSession('${s.session_id}', event)">
                <div class="h-title" title="${s.workspace}">📁 ${title}</div>
                <div class="h-date">${timeAgo(s.updated_at)}</div>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center">
                ${urlBadge}
            </div>
            <button class="h-del-btn" onclick="deleteSession('${s.session_id}', event)" title="Delete session">🗑</button>
        `;
        frag.appendChild(d);
    });
    h.appendChild(frag);
}

function loadSession(sid, e) {
    if (e) e.stopPropagation();
    if (confirm('Load this session?')) ws.send(JSON.stringify({type:'load_history', session_id: sid}));
}
function deleteSession(sid, e) {
    if (e) e.stopPropagation();
    if (confirm('Delete this session permanently?')) ws.send(JSON.stringify({type:'delete_history', session_id: sid}));
}

function showToast(msg, duration=2500) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove('show'), duration);
}

function saveSession() {
    ws.send(JSON.stringify({type: 'save_session'}));
}

// ===== SEND / STOP TOGGLE =====
let agentRunning = false;

function setAgentRunning(running) {
    agentRunning = running;
    const btn = document.getElementById('sendBtn');
    const prompt = document.getElementById('prompt');
    const chat = document.getElementById('chat');
    
    let loader = document.getElementById('globalLoader');
    if (!loader) {
        loader = document.createElement('div');
        loader.id = 'globalLoader';
        loader.style.cssText = 'padding:12px;display:flex;align-items:center;gap:10px;font-size:12px;color:var(--text-dim);';
        loader.innerHTML = `<span class="pulse-icon" style="color:#f59e0b">●</span> <span>Working...</span>`;
    }
    
    if (running) {
        btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor"><rect x="2" y="2" width="10" height="10" rx="2"/></svg>`;
        btn.title = 'Stop agent';
        btn.style.background = '#c0392b';
        btn.style.animation = 'none';
        prompt.disabled = true;
        prompt.style.opacity = '0.5';
        
        loader.style.display = 'flex';
        chat.appendChild(loader);
        chat.scrollTop = 99999;
    } else {
        btn.innerHTML = '➤';
        btn.title = 'Send';
        btn.style.background = '';
        btn.style.animation = '';
        prompt.disabled = false;
        prompt.style.opacity = '';
        // Clear any lingering review card
        document.getElementById('reviewActionBar').style.display = 'none';
        
        if (loader.parentNode) loader.parentNode.removeChild(loader);
    }
}

function handleSendStop() {
    if (agentRunning) {
        // STOP: tell backend to kill the task + subprocess
        ws.send(JSON.stringify({type: 'stop_agent'}));
        setAgentRunning(false);
        // Add a stopped notice in chat
        const note = document.createElement('div');
        note.className = 'chat-entry';
        note.innerHTML = `<div style="color:#f59e0b;font-size:12px;padding:6px 0">⏹ Agent stopped by user.</div>`;
        document.getElementById('chat').appendChild(note);
        document.getElementById('chat').scrollTop = 99999;
    } else {
        sendMessage();
    }
}

function sendMessage() {
    const i=document.getElementById('prompt'); const v=i.value.trim();
    if(!v&&pendingImages.length===0)return;
    renderMsg('user',v,pendingImages);
    ws.send(JSON.stringify({type:'message',content:v,images:pendingImages}));
    thinkStartTime = Date.now(); // Start timing the first thought
    i.value=''; i.style.height='36px'; pendingImages=[]; document.getElementById('imagePreviews').innerHTML='';
    setAgentRunning(true);
}

// Auto-grow textarea + Enter to send, Shift+Enter for newline
document.getElementById('prompt').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSendStop();
    }
});
document.getElementById('prompt').addEventListener('input', function() {
    this.style.height = '36px';
    this.style.height = Math.min(this.scrollHeight, 160) + 'px';
});

function sendDirectCommand() {
    const i=document.getElementById('termInput'); const v=i.value.trim(); if(!v)return;
    const prompt=document.getElementById('termPromptDisplay').innerText;
    logToActiveTerminal(`<span style="color:var(--success)">${prompt}</span> <span style="color:#c8d3f5">${v}</span>`);
    ws.send(JSON.stringify({type:'direct_command',command:v})); i.value='';
}
document.getElementById('termInput').addEventListener('keydown',e=>{if(e.key==='Enter')sendDirectCommand();});

function toggleTerm(){document.body.classList.toggle('terminal-hidden');}
function resetAgent(){if(confirm('Reset session?')){ws.send(JSON.stringify({type:'reset'}));location.reload();}}
document.getElementById('toggleTermBtn').onclick=toggleTerm;
document.getElementById('openFolderBtn').onclick=()=>ws.send(JSON.stringify({type:'pick_folder'}));
document.addEventListener('keydown',e=>{if(e.ctrlKey&&e.key==='`'){e.preventDefault();toggleTerm();}});

// ===== RESIZERS =====
function setupVResizer(id, getTarget, side){
    document.getElementById(id).addEventListener('mousedown',()=>{
        const t=getTarget();
        function m(ev){const w=side==='left'?ev.clientX:window.innerWidth-ev.clientX;if(w>150&&w<750)t.style.width=w+'px';}
        document.addEventListener('mousemove',m);
        document.addEventListener('mouseup',()=>document.removeEventListener('mousemove',m),{once:true});
    });
}
setupVResizer('resizerV1',()=>document.getElementById('sidebarEl'),'left');
setupVResizer('resizerV2',()=>document.getElementById('chatEl'),'right');
document.getElementById('resizerH').addEventListener('mousedown',()=>{
    function m(ev){const h=window.innerHeight-ev.clientY;if(h>60&&h<600)document.getElementById('terminalPanel').style.height=h+'px';}
    document.addEventListener('mousemove',m);
    document.addEventListener('mouseup',()=>document.removeEventListener('mousemove',m),{once:true});
});
