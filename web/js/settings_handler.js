const SettingsHandler = {
    loadAndApply() {
        const pathDisplay = document.getElementById('default-download-path-display');
        const setPathBtn = document.getElementById('set-default-download-path-btn');
        const useDefaultToggle = document.getElementById('use-default-download-path-toggle');
        const concurrencyLimitSelect = document.getElementById('concurrency-limit');

        // Concurrency Limit
        const savedLimit = localStorage.getItem('concurrencyLimit');
        concurrencyLimitSelect.value = savedLimit || '3';
        TransferManager.setConcurrencyLimit(parseInt(concurrencyLimitSelect.value, 10));

        // Default Download Path Toggle State
        const useDefault = localStorage.getItem('useDefaultDownloadPath') === 'true';
        useDefaultToggle.checked = useDefault;
        
        // Path display and button state
        const savedPath = localStorage.getItem('defaultDownloadPath');
        if (savedPath) {
            pathDisplay.textContent = savedPath;
            pathDisplay.title = savedPath;
        } else {
            pathDisplay.textContent = '尚未設定';
            pathDisplay.title = '';
        }
        
        setPathBtn.disabled = !useDefault;
        pathDisplay.style.opacity = useDefault ? '1' : '0.5';
    },

    save() {
        const concurrencyLimitSelect = document.getElementById('concurrency-limit');
        // Concurrency Limit
        const limit = concurrencyLimitSelect.value;
        localStorage.setItem('concurrencyLimit', limit);
        TransferManager.setConcurrencyLimit(parseInt(limit, 10));

        // Default Download Path Toggle State
        const useDefaultToggle = document.getElementById('use-default-download-path-toggle');
        localStorage.setItem('useDefaultDownloadPath', useDefaultToggle.checked);
        
        // Note: The original file path is saved in the event listener in main.js
        
        UIModals.showAlert('設定已儲存', '您的設定已更新。', 'btn-primary');
        document.getElementById('settings-popover').classList.add('hidden');
    },

    setupEventListeners() {
        document.getElementById('save-settings-btn').addEventListener('click', () => this.save());

        document.getElementById('set-default-download-path-btn').addEventListener('click', async () => {
            UIManager.toggleModal('blocking-overlay', true);
            try {
                const path = await ApiService.selectDirectory("選取預設下載資料夾");
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
