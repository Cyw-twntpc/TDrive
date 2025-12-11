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
        const concurrencyLimitSelect = document.getElementById('concurrency-limit');

        // Apply concurrency limit
        const savedLimit = localStorage.getItem('concurrencyLimit');
        if (savedLimit) {
            concurrencyLimitSelect.value = savedLimit;
        }
        TransferManager.setConcurrencyLimit(parseInt(concurrencyLimitSelect.value, 10));

        // Apply default download path state
        const useDefault = localStorage.getItem('useDefaultDownloadPath') === 'true';
        useDefaultToggle.checked = useDefault;
        
        const savedPath = localStorage.getItem('defaultDownloadPath');
        if (savedPath) {
            pathDisplay.textContent = savedPath;
            pathDisplay.title = savedPath;
        } else {
            pathDisplay.textContent = 'Not set';
            pathDisplay.title = 'No default path set';
        }
        
        // Enable or disable the "Set Path" button based on the toggle.
        setPathBtn.disabled = !useDefault;
        pathDisplay.style.opacity = useDefault ? '1' : '0.5';
    },

    /**
     * Saves the current settings from the UI to localStorage.
     */
    save() {
        const concurrencyLimitSelect = document.getElementById('concurrency-limit');
        const useDefaultToggle = document.getElementById('use-default-download-path-toggle');
        
        // Save concurrency limit
        const limit = concurrencyLimitSelect.value;
        localStorage.setItem('concurrencyLimit', limit);
        TransferManager.setConcurrencyLimit(parseInt(limit, 10));

        // Save default download path toggle state
        localStorage.setItem('useDefaultDownloadPath', useDefaultToggle.checked);
        
        UIModals.showAlert('Settings Saved', 'Your settings have been updated.', 'btn-primary');
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
                const path = await ApiService.selectDirectory("Select Default Download Folder");
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
