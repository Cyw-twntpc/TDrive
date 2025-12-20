/**
 * @fileoverview Manages the application's settings panel, handling the loading,
 * saving, and application of user preferences to and from localStorage.
 */
const SettingsHandler = {
    /**
     * Loads settings from localStorage and applies them to the UI and relevant services.
     */
    loadAndApply() {
        const pathDisplay = document.getElementById('default-download-path-display');
        const setPathBtn = document.getElementById('set-default-download-path-btn');
        const useDefaultToggle = document.getElementById('use-default-download-path-toggle');

        // Apply default download path state
        const useDefault = localStorage.getItem('useDefaultDownloadPath') === 'true';
        useDefaultToggle.checked = useDefault;
        
        const savedPath = localStorage.getItem('defaultDownloadPath');
        if (savedPath) {
            pathDisplay.textContent = savedPath;
            pathDisplay.title = savedPath;
        } else {
            pathDisplay.textContent = '未設定';
            pathDisplay.title = '尚未設定預設路徑';
        }
        
        // Enable or disable the "Set Path" button based on the toggle.
        setPathBtn.disabled = !useDefault;
        pathDisplay.style.opacity = useDefault ? '1' : '0.5';
    },

    /**
     * Saves the current settings from the UI to localStorage.
     */
    save() {
        const useDefaultToggle = document.getElementById('use-default-download-path-toggle');
        
        // Save default download path toggle state
        localStorage.setItem('useDefaultDownloadPath', useDefaultToggle.checked);
        
        UIModals.showAlert('設定已儲存', '您的設定已更新。', 'btn-primary');
        document.getElementById('settings-popover').classList.add('hidden');
    },

    /**
     * Sets up event listeners for all interactive elements within the settings popover.
     */
    setupEventListeners() {
        document.getElementById('save-settings-btn').addEventListener('click', () => this.save());

        document.getElementById('set-default-download-path-btn').addEventListener('click', async () => {
            UIManager.toggleModal('blocking-overlay', true);
            try {
                const path = await ApiService.selectDirectory("選擇預設下載資料夾");
                if (path) {
                    localStorage.setItem('defaultDownloadPath', path);
                    this.loadAndApply(); // Reload and display the new path
                }
            } finally {
                UIManager.toggleModal('blocking-overlay', false);
            }
        });
        
        document.getElementById('use-default-download-path-toggle').addEventListener('change', (e) => {
            const isEnabled = e.target.checked;
            document.getElementById('set-default-download-path-btn').disabled = !isEnabled;
            document.getElementById('default-download-path-display').style.opacity = isEnabled ? '1' : '0.5';
        });
    }
};
