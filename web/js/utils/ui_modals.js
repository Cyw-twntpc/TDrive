/**
 * @fileoverview Provides a simple, Promise-based API for showing standard modal dialogs.
 */
const UIModals = {
    /**
     * Shows a confirmation dialog with OK and Cancel buttons.
     * @param {string} title - The title of the modal.
     * @param {string} message - The message content (can be HTML).
     * @param {string} [okClass='btn-danger'] - The CSS class for the OK button.
     * @returns {Promise<boolean>} A promise that resolves to true if OK is clicked, false otherwise.
     */
    showConfirm(title, message, okClass = 'btn-danger') {
        return new Promise(resolve => {
            const modalId = 'confirm-modal';
            const modal = document.getElementById(modalId);
            modal.querySelector('#confirm-title').textContent = title;
            modal.querySelector('#confirm-message').innerHTML = message;
            const okBtn = modal.querySelector('#confirm-ok-btn');
            okBtn.className = `btn ${okClass}`;

            const onOk = () => { cleanup(); resolve(true); };
            const onClose = () => { cleanup(); resolve(false); };
            
            // Removes event listeners to prevent memory leaks.
            const cleanup = () => {
                UIManager.toggleModal(modalId, false);
                okBtn.removeEventListener('click', onOk);
                modal.querySelector('#confirm-cancel-btn').removeEventListener('click', onClose);
                modal.querySelector('#confirm-close-btn').removeEventListener('click', onClose);
            };

            okBtn.addEventListener('click', onOk);
            modal.querySelector('#confirm-cancel-btn').addEventListener('click', onClose);
            modal.querySelector('#confirm-close-btn').addEventListener('click', onClose);
            
            UIManager.toggleModal(modalId, true);
        });
    },
    
    /**
     * Shows an alert dialog with a single OK button.
     * @param {string} title - The title of the modal.
     * @param {string} message - The message content (can be HTML).
     * @param {string} [okClass='btn-primary'] - The CSS class for the OK button.
     * @returns {Promise<boolean>} A promise that resolves to true when the dialog is closed.
     */
    showAlert(title, message, okClass = 'btn-primary') {
        return new Promise(resolve => {
            const modalId = 'alert-modal';
            const modal = document.getElementById(modalId);
            modal.querySelector('#alert-title').textContent = title;
            modal.querySelector('#alert-message').innerHTML = message;
            const okBtn = modal.querySelector('#alert-ok-btn');
            okBtn.className = `btn ${okClass}`;

            const onOk = () => { cleanup(); resolve(true); };
            
            const cleanup = () => {
                UIManager.toggleModal(modalId, false);
                okBtn.removeEventListener('click', onOk);
                modal.querySelector('#alert-close-btn').removeEventListener('click', onOk);
            };

            okBtn.addEventListener('click', onOk);
            modal.querySelector('#alert-close-btn').addEventListener('click', onOk);
            
            UIManager.toggleModal(modalId, true);
        });
    },
    
    /**
     * Shows a prompt dialog with a text input field.
     * @param {string} title - The title of the modal.
     * @param {string} message - The message displayed above the input.
     * @param {string} [defaultValue=''] - The default value for the input field.
     * @param {Function|null} [asyncValidator=null] - An optional async function that returns {success: boolean, message: string}
     *                                                If success is true, the modal closes and resolves with the value.
     *                                                If success is false, the modal stays open and shows the error message.
     * @returns {Promise<string|null>} A promise that resolves with the input value, or null if cancelled.
     */
    async showPrompt(title, message, defaultValue = '', asyncValidator = null, selectionStrategy = null) {
        return new Promise(resolve => {
            const modalId = 'prompt-modal';
            const modal = document.getElementById(modalId);
            const input = modal.querySelector('#prompt-input');
            const errorEl = modal.querySelector('#prompt-error');
            const okBtn = modal.querySelector('#prompt-ok-btn');
            
            modal.querySelector('#prompt-title').textContent = title;
            modal.querySelector('#prompt-message').textContent = message;
            input.value = defaultValue;
            
            // Reset state
            errorEl.classList.add('hidden');
            errorEl.textContent = '';
            input.classList.remove('input-error', 'shake');
            okBtn.disabled = false;
            okBtn.classList.remove('loading');

            const showError = (msg) => {
                errorEl.textContent = msg;
                errorEl.classList.remove('hidden');
                input.classList.add('input-error', 'shake');
                
                // Remove shake class after animation completes so it can be re-triggered
                setTimeout(() => {
                    input.classList.remove('shake');
                }, 300);
            };

            const onOk = async () => {
                const value = input.value.trim();
                
                // Clear previous errors
                errorEl.classList.add('hidden');
                input.classList.remove('input-error');

                if (!value) {
                    showError('名稱不能為空。');
                    return;
                }

                if (asyncValidator) {
                    okBtn.classList.add('loading');
                    okBtn.disabled = true;
                    try {
                        const result = await asyncValidator(value);
                        okBtn.classList.remove('loading');
                        okBtn.disabled = false;

                        if (result && !result.success) {
                            showError(result.message || '發生未知的驗證錯誤。');
                            input.focus();
                            return; // Keep the prompt open
                        }
                    } catch (e) {
                        okBtn.classList.remove('loading');
                        okBtn.disabled = false;
                        showError('發生系統錯誤，請重試。');
                        console.error(e);
                        return;
                    }
                }
                
                cleanup();
                resolve(value);
            };

            const onClose = () => { cleanup(); resolve(null); };
            const onKeyPress = (e) => { 
                if (e.key === 'Enter') onOk(); 
                // Clear error on any typing
                errorEl.classList.add('hidden');
                input.classList.remove('input-error');
            };

            const cleanup = () => {
                UIManager.toggleModal(modalId, false);
                input.removeEventListener('keydown', onKeyPress);
                okBtn.removeEventListener('click', onOk);
                modal.querySelector('#prompt-cancel-btn').removeEventListener('click', onClose);
                modal.querySelector('#prompt-close-btn').removeEventListener('click', onClose);
            };

            input.addEventListener('keydown', onKeyPress);
            okBtn.addEventListener('click', onOk);
            modal.querySelector('#prompt-cancel-btn').addEventListener('click', onClose);
            modal.querySelector('#prompt-close-btn').addEventListener('click', onClose);

            UIManager.toggleModal(modalId, true);
            input.focus();
            
            if (selectionStrategy === 'filename' && defaultValue.lastIndexOf('.') > 0) {
                input.setSelectionRange(0, defaultValue.lastIndexOf('.'));
            } else {
                input.select();
            }
        });
    }
};