// 全域變數
let tdrive_bridge;
let selectedMethod = 'qr';

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    initWebChannel();
    setupEventListeners();
});

function initWebChannel() {
    if (typeof qt !== 'undefined') {
        new QWebChannel(qt.webChannelTransport, function(channel) {
            window.tdrive_bridge = channel.objects.tdrive_bridge;
            console.log("QWebChannel bridge initialized on login page.");
            
            // 綁定後端事件
            if (window.tdrive_bridge && window.tdrive_bridge.login_event) {
                window.tdrive_bridge.login_event.connect(on_login_event);
            }
        });
    } else {
        console.warn("qt object not found. UI testing mode.");
    }
}

// 事件監聽設定
function setupEventListeners() {
    // 視窗控制
    document.getElementById('btn-minimize').addEventListener('click', () => {
        if (window.tdrive_bridge) window.tdrive_bridge.minimize_window();
    });
    
    document.getElementById('btn-close').addEventListener('click', () => {
        if (window.tdrive_bridge) window.tdrive_bridge.close_window();
    });

    // 按鈕動作
    document.getElementById('submitApiBtn').addEventListener('click', submitApiCredentials);
    document.getElementById('proceedBtn').addEventListener('click', proceedWithMethod);
    document.getElementById('refreshQrBtn').addEventListener('click', startQrLogin);
    document.getElementById('submitPhoneBtn').addEventListener('click', submitPhoneNumber);
    document.getElementById('submitCodeBtn').addEventListener('click', submitVerificationCode);
    document.getElementById('submitPasswordBtn').addEventListener('click', submitPassword);

    // 登入方式切換
    document.querySelectorAll('.method-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            selectMethod(e.currentTarget.dataset.method);
        });
    });

    // 畫面切換 (返回按鈕等)
    document.querySelectorAll('[data-target]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            showScreen(e.currentTarget.dataset.target);
        });
    });

    // 輸入框 Enter 支援
    const addEnterSupport = (inputId, btnId) => {
        document.getElementById(inputId).addEventListener('keypress', (e) => {
            if (e.key === 'Enter') document.getElementById(btnId).click();
        });
    };
    addEnterSupport('apiId', 'submitApiBtn');
    addEnterSupport('apiHash', 'submitApiBtn');
    addEnterSupport('phoneNumber', 'submitPhoneBtn');
    addEnterSupport('verificationCode', 'submitCodeBtn');
    addEnterSupport('password', 'submitPasswordBtn');

    let isDragging = false;
    
    document.addEventListener('mousedown', (e) => {
        const dragArea = document.querySelector('.window-drag-area');
        if (dragArea && dragArea.contains(e.target)) {
            isDragging = true;
            if (window.tdrive_bridge) {
                // 通知後端開始拖曳，傳送當前滑鼠全域座標
                window.tdrive_bridge.handle_drag_start(e.screenX, e.screenY);
            }
            e.preventDefault();
        }
    });
    
    document.addEventListener('mousemove', (e) => {
        if (isDragging && window.tdrive_bridge) {
            // 持續更新滑鼠位置
            window.tdrive_bridge.handle_drag_move(e.screenX, e.screenY);
        }
    });
    
    document.addEventListener('mouseup', () => {
        if (isDragging) {
            isDragging = false;
            if (window.tdrive_bridge) {
                // 通知後端結束拖曳
                window.tdrive_bridge.handle_drag_end();
            }
        }
    });
}

// UI 輔助函式
const UIHandler = {
    showInlineError: (inputId, message) => {
        const errorEl = document.getElementById(`${inputId}Error`);
        const inputEl = document.getElementById(inputId);
        if (errorEl) {
            errorEl.textContent = message ? `⚠ ${message}` : '';
            // 使用 class 切換 opacity 來顯示/隱藏
            if (message) errorEl.classList.add('show');
            else errorEl.classList.remove('show');
        }
        if (inputEl) {
            inputEl.classList.toggle('error', !!message);
        }
    },
    handleBackendError: (response) => {
        alert(response.message || '發生未知錯誤。');
    }
};

function showScreen(screenId) {
    document.querySelectorAll('.screen').forEach(el => el.classList.remove('active'));
    document.getElementById(screenId)?.classList.add('active');
}

function setLoading(btnId, loading) {
    const btn = document.getElementById(btnId);
    if (btn) btn.classList.toggle('loading', loading);
}

function selectMethod(method) {
    selectedMethod = method;
    document.querySelectorAll('.method-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelector(`.method-btn[data-method="${method}"]`)?.classList.add('active');
}

// 業務邏輯
function submitApiCredentials() {
    ['apiId', 'apiHash'].forEach(id => UIHandler.showInlineError(id, ''));
    const apiId = document.getElementById('apiId').value.trim();
    const apiHash = document.getElementById('apiHash').value.trim();
    let hasError = false;

    if (!apiId || !/^\d+$/.test(apiId)) { UIHandler.showInlineError('apiId', '請輸入有效的 API ID (數字)'); hasError = true; }
    if (!apiHash) { UIHandler.showInlineError('apiHash', '請輸入 API Hash'); hasError = true; }
    if (hasError) return;

    setLoading('submitApiBtn', true);
    if(window.tdrive_bridge) {
        window.tdrive_bridge.verify_api_credentials(Number(apiId), apiHash, function(result) {
            setLoading('submitApiBtn', false);
            if (result.success) {
                if (result.authorized) {
                    loginSuccess();
                } else {
                    showScreen('methodScreen');
                }
            } else {
                UIHandler.showInlineError('apiId', result.message);
            }
        });
    } else {
        // UI 測試模式
        setTimeout(() => { setLoading('submitApiBtn', false); showScreen('methodScreen'); }, 500);
    }
}

function proceedWithMethod() {
    showScreen(selectedMethod + 'Screen');
    if (selectedMethod === 'qr') {
        startQrLogin();
    }
}

function startQrLogin() {
    const qrContainer = document.getElementById('qrCodeContainer');
    const refreshBtn = document.getElementById('refreshQrBtn');
    
    qrContainer.innerHTML = '<div class="qr-loading"><i class="fas fa-spinner"></i><span>產生中...</span></div>';
    refreshBtn.disabled = true;
    refreshBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 產生中';
    
    if(window.tdrive_bridge) {
        window.tdrive_bridge.start_qr_login(function(result) {
            if (result.success) {
                qrContainer.innerHTML = `<img src="${result.qr_url}" alt="QR Code" style="width: 100%; height: 100%; object-fit: contain;">`;
            } else {
                qrContainer.innerHTML = `<div class="qr-loading"><i class="fas fa-times-circle" style="color: #dc3545;"></i><span>${result.message}</span></div>`;
            }
            refreshBtn.disabled = false;
            refreshBtn.innerHTML = '<i class="fas fa-sync-alt"></i> 重試';
        });
    }
}

function on_login_event(event) {
    console.log('Login Event Received:', event);
    switch (event.status) {
        case 'completed': loginSuccess(); break;
        case 'password_needed': showScreen('passwordScreen'); break;
        default:
            const qrContainer = document.getElementById('qrCodeContainer');
            if(qrContainer) {
                qrContainer.innerHTML = `<div class="qr-loading"><i class="fas fa-times-circle" style="color: #ffc107;"></i><span>${event.error || '登入失敗'}</span></div>`;
            }
            break;
    }
}        

function submitPhoneNumber() {
    UIHandler.showInlineError('phoneNumber', '');
    const phone = document.getElementById('phoneNumber').value.trim();
    if (!phone) { UIHandler.showInlineError('phoneNumber', '請輸入手機號碼'); return; }

    setLoading('submitPhoneBtn', true);
    if(window.tdrive_bridge) {
        window.tdrive_bridge.send_code_request(phone, function(result) {
            setLoading('submitPhoneBtn', false);
            if (result.success) {
                document.getElementById('verificationMessage').textContent = `驗證碼已發送到 ${phone}`;
                showScreen('verificationScreen');
            } else {
                UIHandler.showInlineError('phoneNumber', result.message);
            }
        });
    }
}

function submitVerificationCode() {
    UIHandler.showInlineError('verificationCode', '');
    const code = document.getElementById('verificationCode').value.trim();
    if (!code) { UIHandler.showInlineError('verificationCode', '請輸入驗證碼'); return; }

    setLoading('submitCodeBtn', true);
    if(window.tdrive_bridge) {
        window.tdrive_bridge.submit_verification_code(code, function(result) {
            setLoading('submitCodeBtn', false);
            if (result.success) {
                if (result.password_needed) {
                    showScreen('passwordScreen');
                } else {
                    loginSuccess();
                }
            } else {
                UIHandler.showInlineError('verificationCode', result.message);
            }
        });
    }
}

function submitPassword() {
    UIHandler.showInlineError('password', '');
    const password = document.getElementById('password').value.trim();
    if (!password) { UIHandler.showInlineError('password', '請輸入密碼'); return; }
    
    setLoading('submitPasswordBtn', true);
    if(window.tdrive_bridge) {
        window.tdrive_bridge.submit_password(password, function(result) {
            setLoading('submitPasswordBtn', false);
            if (result.success) {
                loginSuccess();
            } else {
                UIHandler.showInlineError('password', result.message);
            }
        });
    }
}

function loginSuccess() {
    showScreen('successScreen');
    setTimeout(() => {
        if(window.tdrive_bridge) {
            window.tdrive_bridge.perform_post_login_initialization(function(result) {
                if (result.success) {
                    window.location.href = 'index.html';
                } else {
                    UIHandler.handleBackendError(result);
                    showScreen('methodScreen');
                }
            });
        }
    }, 500);
}