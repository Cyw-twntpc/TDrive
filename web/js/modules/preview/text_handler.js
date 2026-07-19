const TextHandler = {
    fileId: null,
    _fileName: null,
    _totalPages: 0,
    _renderedPages: [], 
    _isLast: false,
    _language: null,
    _isPlainText: false,
    _loading: false,
    _toolbarWasVisible: false,
    _fragments: null, 
    _livePreviewToken: 0,

    openPreview(fileId, fileName) {
        const toolbar = document.getElementById('file-floating-toolbar');
        if (toolbar && toolbar.classList.contains('visible')) {
            toolbar.classList.remove('visible');
            this._toolbarWasVisible = true;
        }
        this.fileId = fileId;
        this._fileName = fileName;
        this._totalPages = 0;
        this._renderedPages = [];
        this._isLast = false;
        this._loading = false;
        this._fragments = new Map();
        this._livePreviewToken = 0;

        const modal = document.getElementById('text-modal');
        const filenameEl = document.getElementById('text-filename');
        const renderState = document.getElementById('text-render-state');

        filenameEl.textContent = fileName || window.t('preview.unknown');
        renderState.scrollTop = 0;

        const codeBlock = document.getElementById('text-code-block');
        codeBlock.textContent = '';
        codeBlock.style.paddingTop = '0px';
        codeBlock.style.paddingBottom = '0px';

        document.getElementById('text-empty-state').classList.add('hidden');
        document.getElementById('text-error-state').classList.add('hidden');
        document.getElementById('text-render-state').classList.add('hidden');
        document.getElementById('text-loading-state').classList.remove('hidden');
        
        this._injectUI();
        this._hideUI();

        modal.classList.remove('hidden');

        this._loading = true;
        ApiService.getPreviewText(fileId).then(response => {
            if (!response || response.content === undefined) {
                this._showError(window.t('preview.err_load_failed'));
                return;
            }
            this._totalPages = response.total_pages;
            this._language = response.language || 'plaintext';
            this._isPlainText = this._language === 'plaintext';
            
            if (this._isPlainText) {
                renderState.classList.add('text-render-state-hidden-scroll');
                this._showUI();
                this._updateStatusBarPct(0);
            } else {
                renderState.classList.remove('text-render-state-hidden-scroll');
            }

            this._loading = false;
            this._renderPageData(0, response.content);
            this._showRender();
        }).catch(err => {
            this._showError(window.t('preview.err_network') + ': ' + err.message);
        });
    },

    _injectUI() {
        if (!document.getElementById('text-status-bar')) {
            const renderState = document.getElementById('text-render-state');
            const parent = renderState.parentElement;
            
            const statusBar = document.createElement('div');
            statusBar.id = 'text-status-bar';
            statusBar.className = 'text-status-bar hidden';
            statusBar.innerHTML = `
                <div class="text-status-bg">
                    <div id="text-status-progress" class="text-status-progress"></div>
                </div>
                <div id="text-status-text" class="text-status-text">0.00%</div>
            `;
            
            const jumpModal = document.createElement('div');
            jumpModal.id = 'text-jump-modal';
            jumpModal.className = 'text-jump-modal hidden';
            jumpModal.innerHTML = `
                <div class="jump-modal-content">
                    <div class="jump-header">Go to Progress</div>
                    <div class="jump-slider-container">
                        <input type="range" id="text-jump-slider" min="0" max="100" step="0.01" value="0">
                        <span id="text-jump-value">0.00%</span>
                    </div>
                    <div class="jump-actions">
                        <button id="text-jump-cancel" class="btn btn-secondary">Cancel</button>
                        <button id="text-jump-ok" class="btn btn-primary">Jump</button>
                    </div>
                </div>
            `;
            
            const style = document.createElement('style');
            style.id = 'text-custom-ui-style';
            style.textContent = `
                .text-status-bar { position: absolute; bottom: 0; left: 0; right: 0; height: 32px; background: rgba(245, 245, 245, 0.95); backdrop-filter: blur(8px); display: flex; align-items: center; cursor: pointer; z-index: 10; border-top: 1px solid rgba(0,0,0,0.05); transition: background 0.2s; }
                .text-status-bar:hover { background: rgba(255, 255, 255, 1); }
                .text-status-text { color: #5f6368; font-size: 12px; margin-left: 12px; margin-right: 16px; width: 50px; text-align: right; font-family: 'Inter', sans-serif; font-weight: 600; letter-spacing: 0.5px; pointer-events: none; }
                .text-status-bg { flex-grow: 1; height: 4px; background: rgba(0, 0, 0, 0.1); margin-left: 16px; position: relative; border-radius: 2px; overflow: hidden; pointer-events: none; }
                .text-status-progress { position: absolute; left: 0; top: 0; bottom: 0; background: linear-gradient(90deg, #1a73e8, #4285f4); width: 0%; transition: width 0.1s ease-out; }
                
                .text-jump-modal { position: absolute; bottom: 45px; left: 50%; transform: translateX(-50%); background: rgba(255, 255, 255, 0.95); backdrop-filter: blur(12px); border: 1px solid rgba(0,0,0,0.1); border-radius: 12px; padding: 20px; width: 320px; box-shadow: 0 8px 32px rgba(0,0,0,0.15); z-index: 20; color: #333; font-family: 'Inter', sans-serif; animation: fadeIn 0.2s ease-out; }
                @keyframes fadeIn { from { opacity: 0; transform: translate(-50%, 10px); } to { opacity: 1; transform: translate(-50%, 0); } }
                .jump-header { font-size: 13px; font-weight: 600; margin-bottom: 18px; text-align: left; color: #5f6368; text-transform: uppercase; letter-spacing: 1px; }
                .jump-slider-container { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }
                .jump-slider-container input { flex-grow: 1; cursor: pointer; accent-color: #1a73e8; }
                .jump-slider-container span { font-size: 14px; font-weight: 600; color: #1a73e8; width: 55px; text-align: right; }
                .jump-actions { display: flex; justify-content: flex-end; gap: 10px; }
                .jump-actions button { padding: 8px 16px; font-size: 13px; font-weight: 600; border: none; border-radius: 6px; cursor: pointer; transition: all 0.15s; }
                .jump-actions #text-jump-cancel { background: transparent; color: #5f6368; }
                .jump-actions #text-jump-cancel:hover { background: rgba(0,0,0,0.05); color: #333; }
                .jump-actions #text-jump-ok { background: #1a73e8; color: #fff; }
                .jump-actions #text-jump-ok:hover { background: #1557b0; }
                
                .text-render-state-hidden-scroll::-webkit-scrollbar { display: none; }
                .text-render-state-hidden-scroll { -ms-overflow-style: none; scrollbar-width: none; overflow-y: auto !important; }
            `;
            
            parent.appendChild(statusBar);
            parent.appendChild(jumpModal);
            document.head.appendChild(style);

            statusBar.addEventListener('click', () => {
                jumpModal.classList.remove('hidden');
                const slider = document.getElementById('text-jump-slider');
                const currentPct = this._calculateContinuousProgress();
                slider.value = currentPct.toFixed(2);
                this._originalJumpPct = currentPct;
                document.getElementById('text-jump-value').textContent = slider.value + '%';
            });

            let debounceTimer;
            document.getElementById('text-jump-slider')?.addEventListener('input', (e) => {
                const val = e.target.value;
                document.getElementById('text-jump-value').textContent = parseFloat(val).toFixed(2) + '%';
                
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {
                    this._handleLiveJump(parseFloat(val));
                }, 50);
            });

            document.getElementById('text-jump-cancel')?.addEventListener('click', () => {
                jumpModal.classList.add('hidden');
                if (this._originalJumpPct !== undefined) {
                    const slider = document.getElementById('text-jump-slider');
                    slider.value = this._originalJumpPct.toFixed(2);
                    this._handleLiveJump(this._originalJumpPct);
                }
            });

            document.getElementById('text-jump-ok')?.addEventListener('click', () => {
                jumpModal.classList.add('hidden');
            });
        }
    },

    _showUI() {
        const sb = document.getElementById('text-status-bar');
        if (sb) sb.classList.remove('hidden');
    },

    _hideUI() {
        const sb = document.getElementById('text-status-bar');
        const jm = document.getElementById('text-jump-modal');
        if (sb) sb.classList.add('hidden');
        if (jm) jm.classList.add('hidden');
    },

    _calculateContinuousProgress() {
        if (!this._isPlainText || this._totalPages <= 0) return 0;
        if (this._totalPages === 1) return 100;
        
        const container = document.getElementById('text-render-state');
        const containerRect = container.getBoundingClientRect();
        
        let activePage = this._renderedPages[0];
        let fraction = 0;
        
        for (const pageIdx of this._renderedPages) {
            const pageEl = this._fragments.get(pageIdx);
            if (pageEl) {
                const rect = pageEl.getBoundingClientRect();
                if (rect.top <= containerRect.top && rect.bottom > containerRect.top) {
                    activePage = pageIdx;
                    fraction = (containerRect.top - rect.top) / rect.height;
                    break;
                } else if (rect.top > containerRect.top && pageIdx === this._renderedPages[0]) {
                    // Very first rendered page is completely below top of container (rubber banding)
                    activePage = pageIdx;
                    fraction = 0;
                    break;
                }
            }
        }
        
        fraction = Math.max(0, Math.min(1, fraction));
        const continuousPage = activePage + fraction;
        let pct = (continuousPage / (this._totalPages)) * 100;
        
        if (container.scrollHeight - container.scrollTop - container.clientHeight <= 1) {
            if (this._renderedPages.includes(this._totalPages - 1)) {
                pct = 100;
            }
        }
        
        return Math.max(0, Math.min(100, pct));
    },

    _updateStatusBarPct(pct) {
        if (!this._isPlainText) return;
        const progress = document.getElementById('text-status-progress');
        const text = document.getElementById('text-status-text');
        if (progress) progress.style.width = pct + '%';
        if (text) text.textContent = pct.toFixed(2) + '%';
    },

    _handleLiveJump(percentage) {
        if (this._totalPages === 0) return;
        
        const exactPage = (percentage / 100) * this._totalPages;
        const targetPage = Math.max(0, Math.min(this._totalPages - 1, Math.floor(exactPage)));
        const fraction = Math.max(0, Math.min(1, exactPage - targetPage));
        
        if (this._renderedPages.includes(targetPage)) {
            const pageEl = this._fragments.get(targetPage);
            if (pageEl) {
                const container = document.getElementById('text-render-state');
                const targetScroll = pageEl.offsetTop + (fraction * pageEl.offsetHeight);
                container.scrollTop = targetScroll;
                return;
            }
        }
        
        this._livePreviewToken++;
        const currentToken = this._livePreviewToken;
        
        this._loading = true;
        
        ApiService.getTextPage(this.fileId, targetPage).then(response => {
            if (currentToken !== this._livePreviewToken) return;
            this._livePreviewToken++;
            this._loading = false;
            
            if (response && response.content !== undefined) {
                const codeBlock = document.getElementById('text-code-block');
                codeBlock.innerHTML = '';
                this._fragments.clear();
                this._renderedPages = [];
                
                const container = document.getElementById('text-render-state');
                container.scrollTop = 0;
                
                this._renderPageData(targetPage, response.content);
                
                const pageEl = this._fragments.get(targetPage);
                if (pageEl) {
                    container.scrollTop = pageEl.offsetTop + (fraction * pageEl.offsetHeight);
                }
            }
        }).catch(() => {
            if (currentToken === this._livePreviewToken) {
                this._loading = false;
            }
        });
    },

    closePreview() {
        const modal = document.getElementById('text-modal');
        modal.classList.add('hidden');
        this.fileId = null;
        this._fragments = null;
        this._renderedPages = [];
        this._hideUI();
        if (this._toolbarWasVisible) {
            const toolbar = document.getElementById('file-floating-toolbar');
            if (toolbar) toolbar.classList.add('visible');
            this._toolbarWasVisible = false;
        }
    },

    _showLoading() {
        document.getElementById('text-loading-state').classList.remove('hidden');
        document.getElementById('text-render-state').classList.add('hidden');
        document.getElementById('text-error-state').classList.add('hidden');
    },

    _showError(msg) {
        document.getElementById('text-loading-state').classList.add('hidden');
        document.getElementById('text-render-state').classList.add('hidden');
        document.getElementById('text-error-state').classList.remove('hidden');
        document.getElementById('text-error-msg').textContent = msg;
        this._loading = false;
    },

    _showRender() {
        document.getElementById('text-loading-state').classList.add('hidden');
        document.getElementById('text-error-state').classList.add('hidden');
        document.getElementById('text-render-state').classList.remove('hidden');
    },
    
    _createPageElement(pageIndex, content) {
        const pageDiv = document.createElement('div');
        pageDiv.dataset.page = pageIndex;
        
        const fragment = document.createDocumentFragment();

        if (this._isPlainText) {
            pageDiv.className = 'text-preview-plaintext';
            const lines = content.replace(/\r\n/g, '\n').split('\n');
            if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
            for (const line of lines) {
                const div = document.createElement('div');
                div.className = 'text-line';
                div.textContent = line;
                fragment.appendChild(div);
            }
        } else if (typeof hljs !== 'undefined') {
            const result = hljs.highlight(content, { language: this._language });
            pageDiv.className = 'hljs';
            const htmlLines = result.value.replace(/\r\n/g, '\n').split('\n');
            if (htmlLines.length > 0 && htmlLines[htmlLines.length - 1] === '') htmlLines.pop();
            for (const html of htmlLines) {
                const div = document.createElement('div');
                div.className = 'code-line';
                div.innerHTML = html || '';
                fragment.appendChild(div);
            }
        } else {
            pageDiv.className = '';
            const lines = content.replace(/\r\n/g, '\n').split('\n');
            if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
            for (const line of lines) {
                const div = document.createElement('div');
                div.className = 'code-line';
                div.textContent = line;
                fragment.appendChild(div);
            }
        }
        pageDiv.appendChild(fragment);
        return pageDiv;
    },

    _renderPageData(pageIndex, content, prepend = false) {
        const pageDiv = this._createPageElement(pageIndex, content);
        this._fragments.set(pageIndex, pageDiv);
        
        const codeBlock = document.getElementById('text-code-block');
        const container = document.getElementById('text-render-state');
        
        if (prepend) {
            codeBlock.insertBefore(pageDiv, codeBlock.firstChild);
            this._renderedPages.unshift(pageIndex);
            container.scrollTop += pageDiv.getBoundingClientRect().height;
        } else {
            codeBlock.appendChild(pageDiv);
            this._renderedPages.push(pageIndex);
        }
        
        if (this._renderedPages.length > 5) {
            if (prepend) {
                const toRemove = this._renderedPages.pop();
                const el = this._fragments.get(toRemove);
                if (el && el.parentNode) {
                    el.parentNode.removeChild(el);
                }
                this._fragments.delete(toRemove);
            } else {
                const toRemove = this._renderedPages.shift();
                const el = this._fragments.get(toRemove);
                if (el && el.parentNode) {
                    const h = el.getBoundingClientRect().height;
                    el.parentNode.removeChild(el);
                    container.scrollTop -= h;
                }
                this._fragments.delete(toRemove);
            }
        }
        
        if (this._isPlainText) {
            setTimeout(() => {
                const pct = this._calculateContinuousProgress();
                this._updateStatusBarPct(pct);
                this._checkFillScreen();
            }, 10);
        }
    },

    _checkFillScreen() {
        const el = document.getElementById('text-render-state');
        if (!el || this._loading || this.fileId === null || this._renderedPages.length === 0) return;
        
        const maxPage = Math.max(...this._renderedPages);
        if (el.scrollHeight <= el.clientHeight + 200) {
            if (maxPage >= 0 && maxPage < this._totalPages - 1) {
                this._loadPage(maxPage + 1, false);
            }
        }
    },

    _loadPage(pageIndex, prepend = false) {
        if (this._loading || this.fileId === null) return;
        if (pageIndex < 0 || pageIndex >= this._totalPages) return;
        if (this._renderedPages.includes(pageIndex)) return;

        this._loading = true;
        const currentToken = this._livePreviewToken;
        ApiService.getTextPage(this.fileId, pageIndex).then(response => {
            if (currentToken !== this._livePreviewToken) {
                // If a jump interrupted this load, we must still reset loading state
                // if no active jump is holding the lock. But _handleLiveJump manages the lock.
                return;
            }
            this._loading = false;
            if (!response || response.content === undefined) return;
            this._renderPageData(pageIndex, response.content, prepend);
        }).catch(() => {
            if (currentToken === this._livePreviewToken) {
                this._loading = false;
            }
        });
    },

    _onScroll(event) {
        if (this._loading || this.fileId === null || this._renderedPages.length === 0) return;
        
        const el = event.target;
        const maxPage = Math.max(...this._renderedPages);
        const minPage = Math.min(...this._renderedPages);
        
        if (el.scrollHeight - el.scrollTop - el.clientHeight < 500) {
            if (maxPage < this._totalPages - 1) {
                this._loadPage(maxPage + 1, false);
            }
        }
        
        if (el.scrollTop < 100) {
            if (minPage > 0) {
                this._loadPage(minPage - 1, true);
            }
        }
        
        if (this._isPlainText) {
            const pct = this._calculateContinuousProgress();
            this._updateStatusBarPct(pct);
        }
    },
};

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('text-close-btn')?.addEventListener('click', () => TextHandler.closePreview());
    document.getElementById('text-retry-btn')?.addEventListener('click', () => {
        if (TextHandler.fileId) TextHandler.openPreview(TextHandler.fileId, TextHandler._fileName);
    });
    const renderState = document.getElementById('text-render-state');
    if (renderState) {
        renderState.addEventListener('scroll', (e) => TextHandler._onScroll(e));
    }
});
