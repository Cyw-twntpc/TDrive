/**
 * @fileoverview Defines the global application state object.
 * 
 * This object serves as the single source of truth for the frontend application's
 * state, including navigation, data, selections, and user information.
 */

// --- Global Application State ---
const AppState = {
    // ID of the currently displayed folder.
    currentFolderId: null,
    // Unique ID for the latest view request (folder navigation or search) to prevent race conditions.
    currentViewRequestId: null, 
    // Flat array of all folders for building the tree view.
    folderTreeData: [],
    // Map for quick folder lookups by ID.
    folderMap: new Map(),
    // Contents of the currently displayed folder or search results.
    currentFolderContents: { folders: [], files: [] },
    // Array of currently selected items in the file list.
    selectedItems: [],
    // Current sorting criteria for the file list.
    currentSort: { key: 'name', order: 'asc' },
    // Flag indicating if the UI is currently in search mode.
    isSearching: false,
    // The current search term.
    searchTerm: '',
    // The scope of the current search ('all' or 'current').
    searchScope: 'all',
    // Information about the logged-in user.
    userInfo: null,
    // Base64 data URI of the user's avatar.
    userAvatar: null,
    // --- Drag and Drop State ---
    // Whether a drag operation is currently in progress.
    isDragging: false,
    // Array of items currently being dragged.
    draggedItems: [],
    // Timer ID for folder auto-expansion during drag hover.
    dragHoverTimer: null,
};
