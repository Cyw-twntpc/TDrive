/**
 * @fileoverview A utility object for managing global UI elements and state.
 *
 * This object provides a collection of static methods to control elements like
 * loading indicators, popovers, modals, and to format data for display.
 */
const UIManager = {
    // --- Global Indicators & Locks ---
    
    /** Shows the global top progress bar. */
    startProgress() {
        document.getElementById('global-progress-bar')?.classList.add('visible');
    },

    /** Hides the global top progress bar. */
    stopProgress() {
        document.getElementById('global-progress-bar')?.classList.remove('visible');
    },

    /** Locks the UI to prevent user interaction during critical operations. */
    setInteractionLock(isLocked) {
        document.getElementById('interaction-lock-overlay')?.classList.toggle('visible', isLocked);
    },
    
    // --- UI Formatting Helpers ---

    /**
     * Gets a Font Awesome icon class based on a file's extension.
     * @param {string} fileName - The name of the file.
     * @returns {string} The corresponding Font Awesome class string.
     */
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
    
    /**
     * Formats a number of bytes into a human-readable string (B, KB, MB, GB).
     * @param {number} bytes - The number of bytes.
     * @returns {string} The formatted size string.
     */
    formatBytes(bytes) {
        if (typeof bytes !== 'number' || isNaN(bytes) || bytes <= 0) return '0 B';
        
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        
        // Calculate index, but clamp it to valid array range.
        // Math.log of value < 1 is negative, so we use Math.max(0, ...).
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        const index = Math.max(0, Math.min(i, sizes.length - 1));
        
        return parseFloat((bytes / Math.pow(k, index)).toFixed(1)) + ' ' + sizes[index];
    },

    /**
     * Gets a user-friendly description for a file type based on its extension.
     * @param {string} fileName - The name of the file.
     * @param {boolean} isFolder - True if the item is a folder.
     * @returns {string} The file type description.
     */
    getFileTypeDescription(fileName, isFolder) {
        if (isFolder) return '資料夾';
        const extension = fileName.split('.').pop().toLowerCase();
        if (!fileName.includes('.')) return '檔案';
        switch (extension) {
            case 'txt': return '文字文件';
            case 'md': return 'Markdown 文件';
            case 'pdf': return 'PDF 文件';
            case 'doc': case 'docx': return 'Word 文件';
            case 'xls': case 'xlsx': return 'Excel 試算表';
            case 'ppt': case 'pptx': return 'PowerPoint 簡報';
            case 'zip': case 'rar': case '7z': case 'tar': return `${extension.toUpperCase()} 壓縮檔`;
            case 'jpg': case 'jpeg': return 'JPEG 圖片';
            case 'png': return 'PNG 圖片';
            case 'gif': return 'GIF 圖片';
            case 'mp3': case 'wav': case 'aac': return `${extension.toUpperCase()} 音訊`;
            case 'mp4': case 'mov': case 'avi': case 'mkv': return `${extension.toUpperCase()} 影片`;
            case 'py': return 'Python 腳本';
            case 'js': return 'JavaScript 檔案';
            case 'html': return 'HTML 文件';
            case 'css': return '樣式表';
            case 'json': return 'JSON 檔案';
            case 'exe': return '應用程式';
            default: return `${extension.toUpperCase()} 檔案`;
        }
    },

    // --- Modal & Overlay Control ---

    /** Toggles the visibility of a modal or overlay. */
    toggleModal(modalId, show) {
        document.getElementById(modalId)?.classList.toggle('hidden', !show);
    },

    // --- Popovers & User Info ---

    /** Updates the user avatar icon with either a custom image or a default icon. */
    updateUserAvatar(AppState) {
        const userBtn = document.getElementById('user-btn');
        userBtn.innerHTML = AppState.userAvatar 
            ? `<img src="${AppState.userAvatar}" alt="User Avatar">`
            : `<i class="fas fa-user-circle"></i>`;
    },

    /** Populates the user info popover with data from the AppState. */
    populateUserInfoPopover(AppState) {
        const contentEl = document.getElementById('user-info-content');
        if (AppState.userInfo) {
            const { name, phone, username } = AppState.userInfo;
            contentEl.innerHTML = `<p><strong>名稱：</strong> <span>${name}</span></p>
                                   <p><strong>電話：</strong> <span>${phone}</span></p>
                                   <p><strong>使用者名稱：</strong> <span>${username}</span></p>`;
        } else {
            contentEl.innerHTML = '<p>載入中...</p>';
        }
    },

    /** Sets up global click listeners to handle popover visibility. */
    setupPopovers() {
        document.querySelectorAll('[data-popover]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const popoverId = btn.dataset.popover;
                const targetPopover = document.getElementById(popoverId);
                
                const isVisible = !targetPopover.classList.contains('hidden');
                // Hide all other popovers first.
                document.querySelectorAll('.popover').forEach(p => p.classList.add('hidden'));
                if (!isVisible) {
                    targetPopover.classList.remove('hidden');
                }
            });
        });

        // Clicking anywhere outside a popover will close all of them.
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.popover') && !e.target.closest('[data-popover]')) {
                document.querySelectorAll('.popover').forEach(p => p.classList.add('hidden'));
            }
        });
    },

    // --- Error Handling ---
    
    /** Displays a user-friendly modal alert for backend-reported errors. */
    handleBackendError(response) {
        let title = '錯誤';
        let message = response.message || '發生未知的內部錯誤，請稍後再試。';

        switch (response.error_code) {
            case 'ITEM_ALREADY_EXISTS':
                title = '操作失敗';
                break;
            case 'PATH_NOT_FOUND':
                title = '項目不存在';
                break;
            case 'CONNECTION_FAILED':
                title = '連線錯誤';
                message = '無法連線至伺服器，請檢查您的網路連線並重試。';
                break;
            case 'FLOOD_WAIT_ERROR':
                title = '請求過於頻繁';
                break;
            case 'INVALID_OPERATION':
                title = '無效操作';
                break;
            case 'INTERNAL_ERROR':
                title = '系統錯誤';
                break;
        }
        UIModals.showAlert(title, message, 'btn-primary');
    },

    /** Shows or hides the "Connection Lost" overlay. */
    handleConnectionStatus(status) {
        console.log(`Connection status changed: ${status}`);
        this.toggleModal('connection-lost-overlay', status === 'lost');
    }
};
