const DocumentHandler = {
    fileId: null,
    isFull: false,
    currentBlobUrl: null,
    _loadReqSeq: 0,
    _renderKickTimers: [],

    /**
     * Cancel any pending fallback render-kick timers from previous document loads.
     */
    _clearRenderKickTimers() {
        for (const timerId of this._renderKickTimers) {
            clearTimeout(timerId);
        }
        this._renderKickTimers = [];
    },

    /**
     * Track a bounded timer that cleans itself up upon firing or document switch/close.
     */
    _scheduleRenderKickTimer(fn, delayMs) {
        const timerId = setTimeout(() => {
            this._renderKickTimers = this._renderKickTimers.filter(id => id !== timerId);
            fn();
        }, delayMs);
        this._renderKickTimers.push(timerId);
        return timerId;
    },

    /**
     * Nudge PDF.js viewerContainer scroll position by 1px and restore on the next frame
     * to force Chromium/QtWebEngine compositor to present the initial painted canvas.
     */
    _nudgePdfCompositor(iframe, reqSeq) {
        if (reqSeq !== this._loadReqSeq || !iframe || !iframe.isConnected) return;
        if (iframe._tdriveNudgePending) return;
        try {
            const cw = iframe.contentWindow;
            const cd = iframe.contentDocument;
            const vc = cd && cd.getElementById('viewerContainer');
            if (!cw || !vc) return;

            void vc.offsetHeight;
            if (vc.scrollHeight > vc.clientHeight) {
                const prevTop = vc.scrollTop;
                const delta = (prevTop + 1 <= vc.scrollHeight - vc.clientHeight) ? 1 : -1;
                const targetTop = prevTop + delta;
                iframe._tdriveNudgePending = true;
                vc.scrollTop = targetTop;
                // Read back the actual scrollTop since Chromium fractional DPI scaling quantizes CSS px.
                const appliedTop = vc.scrollTop;

                const scheduleFrame = cw.requestAnimationFrame
                    ? cw.requestAnimationFrame.bind(cw)
                    : (cb) => setTimeout(cb, 0);
                scheduleFrame(() => {
                    iframe._tdriveNudgePending = false;
                    if (reqSeq !== this._loadReqSeq || !iframe.isConnected) return;
                    if (Math.abs(vc.scrollTop - appliedTop) < 0.5) {
                        vc.scrollTop = prevTop;
                    }
                });
            }
        } catch (_) {
            iframe._tdriveNudgePending = false;
            // Ignore cross-origin or detached-frame access errors during teardown.
        }
    },

    /**
     * Trigger PDF.js layout/render evaluation and compositor repaint for the active document.
     */
    _kickPdfViewerRender(iframe, reqSeq, dispatchResize = false) {
        if (reqSeq !== this._loadReqSeq || !iframe || !iframe.isConnected) return false;
        try {
            const cw = iframe.contentWindow;
            const cd = iframe.contentDocument;
            const app = cw && cw.PDFViewerApplication;
            const viewer = app && app.pdfViewer;
            if (!cw || !app || !viewer) return false;

            const firstPage = viewer._pages && viewer._pages[0];
            const pageNumInput = cd && cd.getElementById('pageNumber');
            const isSpinnerActive = !!(pageNumInput && pageNumInput.classList.contains('loading'));
            const isFirstPageFinished = !!(firstPage && firstPage.renderingState === 3 && !isSpinnerActive);

            if (dispatchResize && !isFirstPageFinished) {
                const ResizeEventCtor = cw.Event || Event;
                cw.dispatchEvent(new ResizeEventCtor('resize'));
            }
            if (typeof viewer.update === 'function') {
                viewer.update();
            }
            if (typeof app.forceRendering === 'function') {
                app.forceRendering();
            }
            this._nudgePdfCompositor(iframe, reqSeq);
            return isFirstPageFinished;
        } catch (_) {
            return false;
        }
    },

    /**
     * Hook PDF.js initialization/render lifecycle and bounded watchdogs on the embedded iframe
     * so page 1 renders and presents without waiting for manual scrolling.
     */
    _attachPdfRenderKick(iframe, reqSeq) {
        iframe.addEventListener('load', () => {
            if (reqSeq !== this._loadReqSeq || !iframe.isConnected) return;

            let cw;
            let app;
            try {
                cw = iframe.contentWindow;
                app = cw && cw.PDFViewerApplication;
            } catch (_) {
                return;
            }

            const wireViewer = () => {
                if (reqSeq !== this._loadReqSeq || !iframe.isConnected) return;

                let currentApp;
                let bus;
                try {
                    cw = iframe.contentWindow;
                    currentApp = cw && cw.PDFViewerApplication;
                    bus = currentApp && currentApp.eventBus;
                } catch (_) {
                    return;
                }

                let initialPagePainted = false;
                const subscribe = bus && (
                    typeof bus._on === 'function'
                        ? bus._on.bind(bus)
                        : (typeof bus.on === 'function' ? bus.on.bind(bus) : null)
                );

                if (subscribe) {
                    subscribe('documentinit', () => {
                        this._kickPdfViewerRender(iframe, reqSeq, false);
                    });
                    subscribe('pagesinit', () => {
                        this._kickPdfViewerRender(iframe, reqSeq, false);
                    });
                    subscribe('pagerendered', (evt) => {
                        if (reqSeq !== this._loadReqSeq || !iframe.isConnected) return;
                        if (evt && evt.pageNumber === 1 && !initialPagePainted) {
                            initialPagePainted = true;
                            const scheduleFrame = cw && cw.requestAnimationFrame
                                ? cw.requestAnimationFrame.bind(cw)
                                : (cb) => setTimeout(cb, 0);
                            scheduleFrame(() => {
                                this._nudgePdfCompositor(iframe, reqSeq);
                            });
                        }
                    });
                }

                [150, 450, 1000].forEach((delayMs) => {
                    this._scheduleRenderKickTimer(() => {
                        if (reqSeq !== this._loadReqSeq || !iframe.isConnected) return;
                        try {
                            const doc = iframe.contentDocument;
                            const win = iframe.contentWindow;
                            const v = win && win.PDFViewerApplication && win.PDFViewerApplication.pdfViewer;
                            const p1 = v && v._pages && v._pages[0];
                            const pn = doc && doc.getElementById('pageNumber');
                            const finished = !!(p1 && p1.renderingState === 3 && !(pn && pn.classList.contains('loading')));
                            if (!finished) {
                                this._kickPdfViewerRender(iframe, reqSeq, true);
                            }
                        } catch (_) {
                            // Ignore transient frame states during navigation/close.
                        }
                    }, delayMs);
                });
            };

            if (app && app.initializedPromise && typeof app.initializedPromise.then === 'function') {
                app.initializedPromise.then(wireViewer).catch(() => {});
            } else {
                wireViewer();
            }
        }, { once: true });
    },

    openDocument(fileId) {
        this._clearRenderKickTimers();
        const reqSeq = ++this._loadReqSeq;
        this.fileId = fileId;
        this.isFull = false;
        const modal = document.getElementById('document-modal');
        const filenameEl = document.getElementById('doc-filename');
        const emptyState = document.getElementById('doc-empty-state');
        const loadingState = document.getElementById('doc-loading-state');
        const renderState = document.getElementById('doc-render-state');
        const errorState = document.getElementById('doc-error-state');

        emptyState.classList.add('hidden');
        errorState.classList.add('hidden');
        renderState.classList.add('hidden');
        loadingState.classList.remove('hidden');
        modal.classList.remove('hidden');

        const toolbar = document.getElementById('file-floating-toolbar');
        if (toolbar) toolbar.style.display = 'none';

        ApiService.getPreviewFile(fileId).then(response => {
            if (reqSeq !== this._loadReqSeq || this.fileId !== fileId) return;
            if (!response || !response.base64_data) {
                this._showError(window.t('preview.err_doc_load_failed'));
                return;
            }
            filenameEl.textContent = response.file_name || window.t('preview.unknown');
            this.isFull = response.is_full;
            this._renderBase64Pdf(response.base64_data);
        }).catch(err => {
            if (reqSeq !== this._loadReqSeq || this.fileId !== fileId) return;
            this._showError(window.t('preview.err_doc_load_failed') + ': ' + err.message);
        });
    },

    closeDocument() {
        this._clearRenderKickTimers();
        this._loadReqSeq += 1;
        if (this.currentBlobUrl) {
            URL.revokeObjectURL(this.currentBlobUrl);
            this.currentBlobUrl = null;
        }
        const modal = document.getElementById('document-modal');
        const embed = document.getElementById('doc-embed');
        if (embed) embed.src = '';
        modal.classList.add('hidden');
        this.fileId = null;

        const filenameEl = document.getElementById('doc-filename');
        if (filenameEl) filenameEl.textContent = '';

        const toolbar = document.getElementById('file-floating-toolbar');
        if (toolbar) toolbar.style.display = '';
    },

    _showLoading() {
        this._clearRenderKickTimers();
        document.getElementById('doc-loading-state').classList.remove('hidden');
        document.getElementById('doc-render-state').classList.add('hidden');
        document.getElementById('doc-error-state').classList.add('hidden');
    },

    _showError(msg) {
        this._clearRenderKickTimers();
        document.getElementById('doc-loading-state').classList.add('hidden');
        document.getElementById('doc-render-state').classList.add('hidden');
        document.getElementById('doc-error-state').classList.remove('hidden');
        document.getElementById('doc-error-msg').textContent = msg;
    },

    _showRender(url) {
        this._clearRenderKickTimers();
        document.getElementById('doc-loading-state').classList.add('hidden');
        document.getElementById('doc-error-state').classList.add('hidden');
        const renderState = document.getElementById('doc-render-state');
        renderState.classList.remove('hidden');

        const oldEmbed = document.getElementById('doc-embed');
        if (oldEmbed) {
            oldEmbed.remove();
        }

        const newEmbed = document.createElement('iframe');
        newEmbed.id = 'doc-embed';
        newEmbed.width = '100%';
        newEmbed.height = '100%';
        newEmbed.style.border = 'none';

        const footerBar = document.getElementById('doc-footer-bar');
        renderState.insertBefore(newEmbed, footerBar);

        if (!this.isFull) {
            document.getElementById('doc-footer-bar').classList.remove('hidden');
        } else {
            document.getElementById('doc-footer-bar').classList.add('hidden');
        }

        // Flush layout so the iframe has non-zero dimensions before PDF.js initializes.
        void renderState.offsetHeight;
        this._attachPdfRenderKick(newEmbed, this._loadReqSeq);
        newEmbed.src = url;
    },

    loadFullDocument() {
        if (!this.fileId) return;
        const targetId = this.fileId;
        const reqSeq = ++this._loadReqSeq;
        this._showLoading();
        ApiService.loadFullDocument(targetId).then(response => {
            if (reqSeq !== this._loadReqSeq || this.fileId !== targetId) return;
            if (!response || !response.base64_data) {
                this._showError(window.t('preview.err_doc_load_failed'));
                return;
            }
            this.isFull = response.is_full;
            this._renderBase64Pdf(response.base64_data);
        }).catch(err => {
            if (reqSeq !== this._loadReqSeq || this.fileId !== targetId) return;
            this._showError(window.t('preview.err_doc_load_failed') + ': ' + err.message);
        });
    },

    _renderBase64Pdf(base64Data) {
        try {
            const binaryString = window.atob(base64Data);
            const len = binaryString.length;
            const bytes = new Uint8Array(len);
            for (let i = 0; i < len; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }
            
            const blob = new Blob([bytes], { type: 'application/pdf' });
            if (this.currentBlobUrl) {
                URL.revokeObjectURL(this.currentBlobUrl);
            }
            this.currentBlobUrl = URL.createObjectURL(blob);
            
            const viewerUrl = 'vendor/pdfjs/web/viewer.html?file=' + encodeURIComponent(this.currentBlobUrl);
            this._showRender(viewerUrl);
        } catch (e) {
            this._showError(window.t('preview.err_doc_load_failed') + ': ' + e.message);
        }
    },
};

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('doc-close-btn')?.addEventListener('click', () => DocumentHandler.closeDocument());
    document.getElementById('doc-retry-btn')?.addEventListener('click', () => {
        if (DocumentHandler.fileId) DocumentHandler.openDocument(DocumentHandler.fileId);
    });
    document.getElementById('doc-load-full-btn')?.addEventListener('click', () => DocumentHandler.loadFullDocument());
});
