const UIManager = {
    escapeHtml(text) {
        if (!text) return '';
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    },

    startProgress() {
        document.getElementById('global-progress-bar')?.classList.add('visible');
    },

    stopProgress() {
        document.getElementById('global-progress-bar')?.classList.remove('visible');
    },

    setInteractionLock(isLocked) {
        document.getElementById('interaction-lock-overlay')?.classList.toggle('visible', isLocked);
    },
    
    getFileTypeIcon(fileName) {
        const extension = fileName.split('.').pop().toLowerCase();
        if (fileName.includes('.') === false) return 'fa-solid fa-file';
        switch (extension) {
            case 'txt': case 'md': return 'fa-solid fa-file-lines';
            case 'pdf': return 'fa-solid fa-file-pdf';
            case 'doc': case 'docx': return 'fa-solid fa-file-word';
            case 'xls': case 'xlsx': return 'fa-solid fa-file-excel';
            case 'ppt': case 'pptx': return 'fa-solid fa-file-powerpoint';
            case 'zip': case 'rar': case '7z': case 'tar': return 'fa-solid fa-file-zipper';
            case 'jpg': case 'jpeg': case 'png': case 'gif': return 'fa-solid fa-file-image';
            case 'mp3': case 'wav': return 'fa-solid fa-file-audio';
            case 'mp4': case 'mov': case 'avi': return 'fa-solid fa-file-video';
            case 'py': case 'js': case 'html': case 'css': case 'json': return 'fa-solid fa-file-code';
            default: return 'fa-solid fa-file';
        }
    },
    
    formatBytes(bytes) {
        if (typeof bytes !== 'number' || isNaN(bytes) || bytes <= 0) return '0 B';
        
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        const index = Math.max(0, Math.min(i, sizes.length - 1));
        
        return parseFloat((bytes / Math.pow(k, index)).toFixed(1)) + ' ' + sizes[index];
    },

    getFileTypeDescription(fileName, isFolder) {
        if (isFolder) return window.t('file_list.type_folder');
        const extension = fileName.split('.').pop().toLowerCase();
        if (!fileName.includes('.')) return window.t('file_list.type_file');
        switch (extension) {
            case 'txt': return window.t('file_list.type_txt');
            case 'md': return window.t('file_list.type_md');
            case 'pdf': return window.t('file_list.type_pdf');
            case 'doc': case 'docx': return window.t('file_list.type_doc');
            case 'xls': case 'xlsx': return window.t('file_list.type_xls');
            case 'ppt': case 'pptx': return window.t('file_list.type_ppt');
            case 'zip': case 'rar': case '7z': case 'tar': return (extension.toUpperCase() + ' ' + window.t('file_list.type_zip'));
            case 'jpg': case 'jpeg': return window.t('file_list.type_jpg');
            case 'png': return window.t('file_list.type_png');
            case 'gif': return window.t('file_list.type_gif');
            case 'mp3': case 'wav': case 'aac': return (extension.toUpperCase() + ' ' + window.t('file_list.type_audio'));
            case 'mp4': case 'mov': case 'avi': case 'mkv': return (extension.toUpperCase() + ' ' + window.t('file_list.type_video'));
            case 'py': return window.t('file_list.type_py');
            case 'js': return window.t('file_list.type_js');
            case 'html': return window.t('file_list.type_html');
            case 'css': return window.t('file_list.type_css');
            case 'json': return window.t('file_list.type_json');
            case 'exe': return window.t('file_list.type_exe');
            default: return (extension.toUpperCase() + ' ' + window.t('file_list.type_unknown'));
        }
    },

    toggleModal(modalId, show) {
        document.getElementById(modalId)?.classList.toggle('hidden', !show);
    },

    updateUserAvatar(AppState) {
        const userBtn = document.getElementById('user-btn');
        userBtn.innerHTML = AppState.userAvatar 
            ? `<img src="${AppState.userAvatar}" alt="User Avatar">`
            : `<i class="fas fa-user-circle"></i>`;
    },

    populateUserInfoPopover(AppState) {
        const contentEl = document.getElementById('user-info-content');
        if (AppState.userInfo) {
            const { name, phone, username } = AppState.userInfo;
            contentEl.innerHTML = `<p><strong data-i18n="dialog.info_name">${window.t('dialog.info_name')}</strong> <span>${UIManager.escapeHtml(name)}</span></p>
                                   <p><strong data-i18n="dialog.info_phone">${window.t('dialog.info_phone')}</strong> <span>${UIManager.escapeHtml(phone)}</span></p>
                                   <p><strong data-i18n="dialog.info_username">${window.t('dialog.info_username')}</strong> <span>${UIManager.escapeHtml(username)}</span></p>`;
        } else {
            contentEl.innerHTML = `<div data-i18n="file_list.loading">${window.t('file_list.loading')}</div>`;
        }
    },

    setupPopovers() {
        document.querySelectorAll('[data-popover]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const popoverId = btn.dataset.popover;
                const targetPopover = document.getElementById(popoverId);
                
                const isVisible = !targetPopover.classList.contains('hidden');
                document.querySelectorAll('.popover').forEach(p => p.classList.add('hidden'));
                if (!isVisible) {
                    targetPopover.classList.remove('hidden');
                }
            });
        });

        document.addEventListener('click', (e) => {
            if (!e.target.closest('.popover') && !e.target.closest('[data-popover]')) {
                document.querySelectorAll('.popover').forEach(p => p.classList.add('hidden'));
            }
        });
    },

    handleBackendError(response) {
        let title = window.t('dialog.err_title');
        let message = response.error_code ? window.t('errors.' + response.error_code) : window.t('dialog.err_internal');

        switch (response.error_code) {
            case 'ITEM_ALREADY_EXISTS':
                title = window.t('dialog.err_op_failed');
                break;
            case 'PATH_NOT_FOUND':
                title = window.t('dialog.err_item_not_found');
                break;
            case 'CONNECTION_FAILED':
                title = window.t('dialog.err_conn');
                message = window.t('dialog.err_conn_msg');
                break;
            case 'FLOOD_WAIT_ERROR':
                title = window.t('dialog.err_too_frequent');
                break;
            case 'INVALID_OPERATION':
                title = window.t('dialog.err_invalid_op');
                break;
            case 'INTERNAL_ERROR':
                title = window.t('dialog.err_sys');
                break;
        }
        UIModals.showAlert(title, message, 'btn-primary');
    },

    handleConnectionStatus(status) {
        this.toggleModal('connection-lost-overlay', status === 'lost');
    }
};
