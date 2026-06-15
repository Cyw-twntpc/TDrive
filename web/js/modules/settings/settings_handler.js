/**
 * @fileoverview Manages the application's settings panel, handling the loading,
 * saving, and application of user preferences to and from the backend bridge.
 */
const SettingsHandler = {
    /**
     * Loads settings from bridge and applies them to the UI and relevant services.
     */
    async loadAndApply() {
        if (!window.tdrive_bridge) {
            console.error('Bridge not ready for SettingsHandler');
            return;
        }
        
        try {
            const settingsStr = await new Promise(resolve => window.tdrive_bridge.get_settings(resolve));
            const settings = JSON.parse(settingsStr);
            
            const pathDisplay = document.getElementById('default-download-path-display');
            const setPathBtn = document.getElementById('set-default-download-path-btn');
            const useDefaultToggle = document.getElementById('use-default-download-path-toggle');
            const langSelect = document.getElementById('language-select');

            // Apply language state
            if (langSelect && window.i18n) {
                langSelect.value = settings.language || 'zh-TW';
            }

            // Apply default download path state
            const useDefault = settings.useDefaultDownloadPath === true || settings.useDefaultDownloadPath === 'true';
            useDefaultToggle.checked = useDefault;
            
            const savedPath = settings.defaultDownloadPath;
            if (savedPath) {
                pathDisplay.textContent = savedPath;
                pathDisplay.title = savedPath;
            } else {
                pathDisplay.textContent = window.t('dialog.path_not_set_val');
                pathDisplay.title = window.t('dialog.path_not_set_msg');
            }
            
            // Enable or disable the "Set Path" button based on the toggle.
            setPathBtn.disabled = useDefault;
            pathDisplay.style.opacity = useDefault ? '0.5' : '1';
        } catch (e) {
            console.error('Failed to load settings', e);
        }
    },

    /**
     * Saves the current settings from the UI to the backend bridge.
     */
    save() {
        const useDefaultToggle = document.getElementById('use-default-download-path-toggle');
        
        // Save default download path toggle state
        if (window.tdrive_bridge) {
            window.tdrive_bridge.save_setting('useDefaultDownloadPath', useDefaultToggle.checked.toString());
        }
        
        UIModals.showAlert(window.t('dialog.settings_saved'), window.t('dialog.settings_saved_msg'), 'btn-primary');
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
                const path = await ApiService.selectDirectory(window.t('dialog.select_default_dl'));
                if (path && window.tdrive_bridge) {
                    window.tdrive_bridge.save_setting('defaultDownloadPath', JSON.stringify(path));
                    this.loadAndApply(); // Reload and display the new path
                }
            } finally {
                UIManager.toggleModal('blocking-overlay', false);
            }
        });
        
        document.getElementById('use-default-download-path-toggle').addEventListener('change', (e) => {
            const isUsingDefault = e.target.checked;
            document.getElementById('set-default-download-path-btn').disabled = isUsingDefault;
            document.getElementById('default-download-path-display').style.opacity = isUsingDefault ? '0.5' : '1';
        });

        const langSelect = document.getElementById('language-select');
        if (langSelect) {
            langSelect.addEventListener('change', async (e) => {
                if (window.i18n) {
                    await window.i18n.changeLanguage(e.target.value);
                }
            });
        }
        
        const restoreBtn = document.getElementById('restore-settings-btn');
        if (restoreBtn) {
            restoreBtn.addEventListener('click', () => {
                if (window.tdrive_bridge) {
                    window.tdrive_bridge.restore_default_settings();
                    this.loadAndApply();
                    if (window.i18n) {
                        window.i18n.init().then(() => {
                            const langSelect = document.getElementById('language-select');
                            if (langSelect) langSelect.value = window.i18n.locale;
                        });
                    }
                    UIModals.showAlert(window.t('dialog.settings_restored'), window.t('dialog.settings_restored_msg'), 'btn-primary');
                    document.getElementById('settings-popover').classList.add('hidden');
                }
            });
        }
        
        // Load initially if bridge is already ready
        if (window.tdrive_bridge) {
            this.loadAndApply();
        } else {
            document.addEventListener('TDriveBridgeReady', () => {
                this.loadAndApply();
            });
        }
    }
};
