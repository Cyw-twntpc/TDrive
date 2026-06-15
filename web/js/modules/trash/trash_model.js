const TrashModel = {

    async restoreItem(item) {
        UIManager.startProgress();
        UIManager.setInteractionLock(true);
        try {
            const result = await ApiService.restoreItems([{ id: item.id, type: item.type }]);
            if (result.success) {
                TrashHandler.loadTrashItems();
            } else {
                UIManager.handleBackendError(result);
            }
        } catch (e) {
            console.error(e);
            UIManager.handleBackendError({ message: window.t('dialog.restore_failed') });
        } finally {
            UIManager.stopProgress();
            UIManager.setInteractionLock(false);
        }
    },

    async deleteItemPermanently(item) {
        const confirmed = await UIModals.showConfirm(
            window.t('trash.btn_delete'),
            window.t('trash.confirm_delete_single').replace('{name}', item.name),
            'btn-danger'
        );
        
        if (!confirmed) return;

        UIManager.startProgress();
        UIManager.setInteractionLock(true);
        try {
            const result = await ApiService.deleteItemsPermanently([{ id: item.id, type: item.type }]);
            if (result.success) {
                TrashHandler.loadTrashItems();
            } else {
                UIManager.handleBackendError(result);
            }
        } catch (e) {
            console.error(e);
            UIManager.handleBackendError({ message: window.t('dialog.delete_failed') });
        } finally {
            UIManager.stopProgress();
            UIManager.setInteractionLock(false);
        }
    },

};
