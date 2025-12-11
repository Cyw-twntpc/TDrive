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

    /** Toggles the visibility of the search spinner. */
    toggleSearchSpinner(show) {
        document.getElementById('search-spinner')?.classList.toggle('visible', show);
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
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    },

    /**
     * Gets a user-friendly description for a file type based on its extension.
     * @param {string} fileName - The name of the file.
     * @param {boolean} isFolder - True if the item is a folder.
     * @returns {string} The file type description.
     */
    getFileTypeDescription(fileName, isFolder) {
        if (isFolder) return 'Folder';
        const extension = fileName.split('.').pop().toLowerCase();
        if (!fileName.includes('.')) return 'File';
        switch (extension) {
            case 'txt': return 'Text Document';
            case 'md': return 'Markdown Document';
            case 'pdf': return 'PDF Document';
            case 'doc': case 'docx': return 'Word Document';
            case 'xls': case 'xlsx': return 'Excel Spreadsheet';
            case 'ppt': case 'pptx': return 'PowerPoint Presentation';
            case 'zip': case 'rar': case '7z': case 'tar': return `${extension.toUpperCase()} Archive`;
            case 'jpg': case 'jpeg': return 'JPEG Image';
            case 'png': return 'PNG Image';
            case 'gif': return 'GIF Image';
            case 'mp3': case 'wav': case 'aac': return `${extension.toUpperCase()} Audio`;
            case 'mp4': case 'mov': case 'avi': case 'mkv': return `${extension.toUpperCase()} Video`;
            case 'py': return 'Python Script';
            case 'js': return 'JavaScript File';
            case 'html': return 'HTML Document';
            case 'css': return 'Stylesheet';
            case 'json': return 'JSON File';
            case 'exe': return 'Application';
            default: return `${extension.toUpperCase()} File`;
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
            contentEl.innerHTML = `<p><strong>Name:</strong> <span>${name}</span></p>
                                   <p><strong>Phone:</strong> <span>${phone}</span></p>
                                   <p><strong>Username:</strong> <span>${username}</span></p>`;
        } else {
            contentEl.innerHTML = '<p>Loading...</p>';
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
        let title = 'Error';
        let message = response.message || 'An unknown internal error occurred. Please try again later.';

        switch (response.error_code) {
            case 'ITEM_ALREADY_EXISTS':
                title = 'Operation Failed';
                break;
            case 'PATH_NOT_FOUND':
                title = 'Item Not Found';
                break;
            case 'CONNECTION_FAILED':
                title = 'Connection Error';
                message = 'Could not connect to the server. Please check your network connection and try again.';
                break;
            case 'FLOOD_WAIT_ERROR':
                title = 'Too Many Requests';
                break;
            case 'INTERNAL_ERROR':
                title = 'System Error';
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
