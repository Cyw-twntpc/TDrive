/**
 * @fileoverview Manages the application's settings panel, handling the loading,
 * saving, and application of user preferences to and from the backend bridge.
 */
const SettingsHandler = {
    /**
     * Applies the selected visual theme to the document root.
     */
    applyTheme(theme) {
        const normalizedTheme = theme === 'dark' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', normalizedTheme);
        if (document.body) {
            document.body.dataset.theme = normalizedTheme;
        }
    },

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

            SettingsHandler.applyTheme(settings.theme || 'light');
            
            const pathDisplay = document.getElementById('default-download-path-display');
            const setPathBtn = document.getElementById('set-default-download-path-btn');
            const useDefaultToggle = document.getElementById('use-default-download-path-toggle');
            const langSelect = document.getElementById('language-select');
            const themeSelect = document.getElementById('theme-select');
            const concurrencySelect = document.getElementById('concurrency-select');

            // Apply language state
            if (langSelect && window.i18n) {
                langSelect.value = settings.language || 'zh-TW';
            }

            if (themeSelect) {
                themeSelect.value = settings.theme === 'dark' ? 'dark' : 'light';
            }

            if (concurrencySelect) {
                const limit = Math.max(1, Math.min(10, parseInt(settings.maxConcurrentTransfers, 10) || 3));
                concurrencySelect.value = String(limit);
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
            setPathBtn.disabled = !useDefault;
            pathDisplay.style.opacity = useDefault ? '1' : '0.5';
        } catch (e) {
            console.error('Failed to load settings', e);
        }
    },

    /**
     * Saves the current settings from the UI to the backend bridge.
     */
    save() {
        const useDefaultToggle = document.getElementById('use-default-download-path-toggle');
        const themeSelect = document.getElementById('theme-select');
        const concurrencySelect = document.getElementById('concurrency-select');
        const selectedTheme = themeSelect && themeSelect.value === 'dark' ? 'dark' : 'light';
        const concurrencyLimit = concurrencySelect
            ? Math.max(1, Math.min(10, parseInt(concurrencySelect.value, 10) || 3))
            : 3;

        SettingsHandler.applyTheme(selectedTheme);

        // Save user settings to the backend bridge
        if (window.tdrive_bridge) {
            window.tdrive_bridge.save_setting('useDefaultDownloadPath', useDefaultToggle.checked.toString());
            if (themeSelect) {
                window.tdrive_bridge.save_setting('theme', JSON.stringify(selectedTheme));
            }
            if (concurrencySelect) {
                window.tdrive_bridge.save_setting('maxConcurrentTransfers', JSON.stringify(concurrencyLimit));
            }
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
            document.getElementById('set-default-download-path-btn').disabled = !isUsingDefault;
            document.getElementById('default-download-path-display').style.opacity = isUsingDefault ? '1' : '0.5';
        });

        const langSelect = document.getElementById('language-select');
        if (langSelect) {
            langSelect.addEventListener('change', async (e) => {
                if (window.i18n) {
                    await window.i18n.changeLanguage(e.target.value);
                }
            });
        }

        const themeSelect = document.getElementById('theme-select');
        if (themeSelect) {
            themeSelect.addEventListener('change', (e) => {
                const selectedTheme = e.target.value === 'dark' ? 'dark' : 'light';
                SettingsHandler.applyTheme(selectedTheme);
                if (window.tdrive_bridge) {
                    window.tdrive_bridge.save_setting('theme', JSON.stringify(selectedTheme));
                }
            });
        }

        const concurrencySelect = document.getElementById('concurrency-select');
        if (concurrencySelect) {
            concurrencySelect.addEventListener('change', (e) => {
                const limit = Math.max(1, Math.min(10, parseInt(e.target.value, 10) || 3));
                e.target.value = String(limit);
                if (window.tdrive_bridge) {
                    window.tdrive_bridge.save_setting('maxConcurrentTransfers', JSON.stringify(limit));
                }
            });
        }
        
        const restoreBtn = document.getElementById('restore-settings-btn');
        if (restoreBtn) {
            restoreBtn.addEventListener('click', () => {
                if (window.tdrive_bridge) {
                    window.tdrive_bridge.restore_default_settings();
                    SettingsHandler.applyTheme('light');
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
