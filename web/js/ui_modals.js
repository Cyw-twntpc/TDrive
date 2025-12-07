const UIModals = {
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
    
    async showPrompt(title, message, defaultValue = '', asyncValidator = null) {
        return new Promise(resolve => {
            const modalId = 'prompt-modal';
            const modal = document.getElementById(modalId);
            const input = modal.querySelector('#prompt-input');
            const errorEl = modal.querySelector('#prompt-error');
            const okBtn = modal.querySelector('#prompt-ok-btn');
            
            modal.querySelector('#prompt-title').textContent = title;
            modal.querySelector('#prompt-message').textContent = message;
            input.value = defaultValue;
            errorEl.classList.add('hidden');
            errorEl.textContent = ''; // 每次開啟時就清除
            okBtn.disabled = false;
            okBtn.classList.remove('loading');

            const onOk = async () => {
                const value = input.value.trim();
                errorEl.classList.add('hidden');
                errorEl.textContent = '';

                if (!value) {
                    errorEl.textContent = '名稱不可為空。';
                    errorEl.style.color = 'var(--danger-color)'; // 強制設定顏色
                    errorEl.classList.remove('hidden');
                    return;
                }

                if (asyncValidator) {
                    okBtn.classList.add('loading');
                    okBtn.disabled = true;
                    const result = await asyncValidator(value);
                    okBtn.classList.remove('loading');
                    okBtn.disabled = false;

                    if (result && !result.success) {
                        errorEl.textContent = result.message || '發生未知驗證錯誤。';
                        errorEl.style.color = 'var(--danger-color)'; // 強制設定顏色
                        errorEl.classList.remove('hidden');
                        return; // 保持彈窗開啟
                    }
                }
                
                cleanup();
                resolve(value);
            };

            const onClose = () => { cleanup(); resolve(null); };
            
            const onKeyPress = (e) => {
                if (e.key === 'Enter') onOk();
            };

            const onInput = () => {
                errorEl.classList.add('hidden');
                errorEl.textContent = '';
            };

            const cleanup = () => {
                UIManager.toggleModal(modalId, false);
                errorEl.textContent = ''; // 關閉時也清除
                input.removeEventListener('keydown', onKeyPress);
                input.removeEventListener('input', onInput);
                okBtn.removeEventListener('click', onOk);
                modal.querySelector('#prompt-cancel-btn').removeEventListener('click', onClose);
                modal.querySelector('#prompt-close-btn').removeEventListener('click', onClose);
            };

            input.addEventListener('keydown', onKeyPress);
            input.addEventListener('input', onInput);
            okBtn.addEventListener('click', onOk);
            modal.querySelector('#prompt-cancel-btn').addEventListener('click', onClose);
            modal.querySelector('#prompt-close-btn').addEventListener('click', onClose);

            UIManager.toggleModal(modalId, true);
            input.focus();
            input.select();
        });
    }
};
