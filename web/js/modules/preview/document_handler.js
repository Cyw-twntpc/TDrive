const DocumentHandler = {
    fileId: null,
    isFull: false,
    currentBlobUrl: null,

    openDocument(fileId) {
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
            if (!response || !response.base64_data) {
                this._showError(window.t('preview.err_doc_load_failed'));
                return;
            }
            filenameEl.textContent = response.file_name || window.t('preview.unknown');
            this.isFull = response.is_full;
            this._renderBase64Pdf(response.base64_data);
        }).catch(err => {
            this._showError(window.t('preview.err_doc_load_failed') + ': ' + err.message);
        });
    },

    closeDocument() {
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
        document.getElementById('doc-loading-state').classList.remove('hidden');
        document.getElementById('doc-render-state').classList.add('hidden');
        document.getElementById('doc-error-state').classList.add('hidden');
    },

    _showError(msg) {
        document.getElementById('doc-loading-state').classList.add('hidden');
        document.getElementById('doc-render-state').classList.add('hidden');
        document.getElementById('doc-error-state').classList.remove('hidden');
        document.getElementById('doc-error-msg').textContent = msg;
    },

    _showRender(url) {
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

        newEmbed.src = url;

        const footerBar = document.getElementById('doc-footer-bar');
        renderState.insertBefore(newEmbed, footerBar);

        if (!this.isFull) {
            document.getElementById('doc-footer-bar').classList.remove('hidden');
        } else {
            document.getElementById('doc-footer-bar').classList.add('hidden');
        }
    },

    loadFullDocument() {
        if (!this.fileId) return;
        this._showLoading();
        ApiService.loadFullDocument(this.fileId).then(response => {
            if (!response || !response.base64_data) {
                this._showError(window.t('preview.err_doc_load_failed'));
                return;
            }
            this.isFull = response.is_full;
            this._renderBase64Pdf(response.base64_data);
        }).catch(err => {
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
