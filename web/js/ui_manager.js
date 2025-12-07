const UIManager = {
    // --- UI Helpers ---
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
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    },

    getFileTypeDescription(fileName, isFolder) {
        if (isFolder) return '檔案資料夾';
        const extension = fileName.split('.').pop().toLowerCase();
        if (fileName.includes('.') === false) return '檔案';
        switch (extension) {
            case 'txt': return '純文字檔案';
            case 'md': return 'Markdown 文件';
            case 'pdf': return 'PDF 文件';
            case 'doc': case 'docx': return 'Word 文件';
            case 'xls': case 'xlsx': return 'Excel 工作表';
            case 'ppt': case 'pptx': return 'PowerPoint 簡報';
            case 'zip': case 'rar': case '7z': case 'tar': return `${extension.toUpperCase()} 壓縮檔`;
            case 'jpg': case 'jpeg': return 'JPEG 影像';
            case 'png': return 'PNG 影像';
            case 'gif': return 'GIF 影像';
            case 'mp3': case 'wav': case 'aac': return `${extension.toUpperCase()} 音訊`;
            case 'mp4': case 'mov': case 'flv': case 'mkv': case 'wmv': case 'avi': return `${extension.toUpperCase()} 影片`;
            case 'py': return 'Python 程式碼';
            case 'js': return 'JavaScript 程式碼';
            case 'html': return 'HTML 文件';
            case 'css': return '網頁樣式表';
            case 'json': return 'JSON 檔案';
            case 'exe': return 'EXE 執行檔';
            default: return `${extension.toUpperCase()} 檔案`;
        }
    },

    // --- Modal & Overlay Control ---
    toggleModal(modalId, show) {
        const modal = document.getElementById(modalId);
        if (modal) {
            if (show) {
                modal.classList.remove('hidden');
            } else {
                modal.classList.add('hidden');
            }
        }
    },

    // --- Popovers & User Info ---
    updateUserAvatar(AppState) {
        const userBtn = document.getElementById('user-btn');
        if (AppState.userAvatar) {
            userBtn.innerHTML = `<img src="${AppState.userAvatar}" alt="avatar">`;
        } else {
            userBtn.innerHTML = `<i class="fas fa-user-circle"></i>`;
        }
    },

    populateUserInfoPopover(AppState) {
        const contentEl = document.getElementById('user-info-content');
        if (AppState.userInfo) {
            const info = AppState.userInfo;
            contentEl.innerHTML = `<p><strong>姓名:</strong> <span>${info.name}</span></p>
                                   <p><strong>電話:</strong> <span>${info.phone}</span></p>
                                   <p><strong>使用者名稱:</strong> <span>${info.username}</span></p>
                                   <p><strong>儲存群組:</strong> <span>${info.storage_group}</span></p>`;
        } else {
            contentEl.innerHTML = '<p>載入中...</p>';
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

    // --- Error Handling ---
    showInlineError(inputId, message) {
        const errorEl = document.getElementById(`${inputId}Error`);
        const inputEl = document.getElementById(inputId);
        if (errorEl) {
            errorEl.textContent = message ? `⚠ ${message}` : '';
            errorEl.classList.toggle('show', !!message);
        }
        if (inputEl) {
            inputEl.classList.toggle('error', !!message);
        }
    },

    handleBackendError(response) {
        let title = '發生錯誤';
        let message = response.message || '發生未知的內部錯誤，請稍後再試。';

        switch (response.error_code) {
            case 'ITEM_ALREADY_EXISTS':
                title = '操作失敗';
                break;
            case 'PATH_NOT_FOUND':
                title = '找不到項目';
                break;
            case 'CONNECTION_FAILED':
                title = '連線錯誤';
                message = '無法連接到伺服器，請檢查您的網路連線或稍後再試。';
                break;
            case 'FLOOD_WAIT_ERROR':
                title = '請求過於頻繁';
                break;
            case 'INTERNAL_ERROR':
                title = '系統內部錯誤';
                break;
        }
        UIModals.showAlert(title, message, 'btn-primary');
    }
};
