// Chat Application JavaScript
// Global state
const state = {
    socket: null,
    currentRoom: 'general',
    currentUser: document.getElementById('current-username').value,
    csrfToken: document.getElementById('csrf-token').value,
    attachments: [],
    replyTo: null,
    contextMessageId: null,
    contextMessageData: null,
    unreadCounts: {},
    typingTimeout: null,
    isTyping: false
};

// Emoji list
const emojis = [
    '😀', '😃', '😄', '😁', '😆', '😅', '🤣', '😂',
    '🙂', '🙃', '😉', '😊', '😇', '🥰', '😍', '🤩',
    '😘', '😗', '😚', '😙', '🥲', '😋', '😛', '😜',
    '🤪', '😝', '🤑', '🤗', '🤭', '🤫', '🤔', '🤐',
    '🤨', '😐', '😑', '😶', '😏', '😒', '🙄', '😬',
    '😮‍💨', '🤥', '😌', '😔', '😪', '🤤', '😴', '😷',
    '🤒', '🤕', '🤢', '🤮', '🤧', '🥵', '🥶', '🥴',
    '😵', '🤯', '🤠', '🥳', '🥸', '😎', '🤓', '🧐',
    '❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '🤍',
    '👍', '👎', '👊', '✊', '🤛', '🤜', '👏', '🙌',
    '👐', '🤲', '🤝', '🙏', '✍️', '💪', '🦾', '🦿',
    '🔥', '⭐', '💫', '✨', '💥', '💢', '💯', '🎉'
];

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initSocket();
    initUI();
    initEventListeners();
    loadEmojis();
    lucide.createIcons();
});

// Socket initialization
function initSocket() {
    state.socket = io({
        transports: ['websocket', 'polling'],
        reconnection: true,
        reconnectionAttempts: 5,
        reconnectionDelay: 1000
    });

    // Connection events
    state.socket.on('connect', () => {
        console.log('Connected to server');
        state.socket.emit('join', { room: state.currentRoom });
    });

    state.socket.on('disconnect', () => {
        console.log('Disconnected from server');
        Toast.show('Connection lost. Reconnecting...', 'warning');
    });

    state.socket.on('reconnect', () => {
        Toast.show('Reconnected!', 'success');
        state.socket.emit('join', { room: state.currentRoom });
    });

    // Message events
    state.socket.on('load_history', (data) => {
        loadMessages(data.messages);
    });

    state.socket.on('message', (data) => {
        addMessage(data);
        if (data.username !== state.currentUser) {
            playNotificationSound();
        }
    });

    state.socket.on('delete_message', (data) => {
        removeMessage(data.message_id);
    });

    state.socket.on('edit_message', (data) => {
        updateMessage(data);
    });

    // User events
    state.socket.on('update_users', (data) => {
        updateUsersList(data.users);
    });

    state.socket.on('user_offline', (data) => {
        updateUserStatus(data.username, false);
    });

    state.socket.on('user_typing', (data) => {
        handleTypingIndicator(data);
    });

    // Notifications
    state.socket.on('notification', (data) => {
        handleNotification(data);
    });

    state.socket.on('update_badge', (data) => {
        updateBadge(data.count);
    });

    // Status messages
    state.socket.on('status', (data) => {
        addStatusMessage(data.msg);
    });

    // Errors
    state.socket.on('error', (data) => {
        Toast.show(data.msg, 'error');
    });
}

// UI initialization
function initUI() {
    // Auto-resize textarea
    const messageInput = document.getElementById('message-input');
    messageInput.addEventListener('input', function() {
        this.style.height = 'auto';
        this.style.height = Math.min(this.scrollHeight, 128) + 'px';
        
        // Character count
        const charCount = document.getElementById('char-count');
        if (this.value.length > 100) {
            charCount.textContent = `${this.value.length}/2000`;
            charCount.classList.remove('hidden');
        } else {
            charCount.classList.add('hidden');
        }
        
        // Typing indicator
        handleTyping();
    });

    // Scroll detection for scroll-to-bottom button
    const messagesContainer = document.getElementById('messages-container');
    messagesContainer.addEventListener('scroll', () => {
        const scrollBtn = document.getElementById('scroll-to-bottom');
        const threshold = 100;
        const isNearBottom = messagesContainer.scrollHeight - messagesContainer.scrollTop - messagesContainer.clientHeight < threshold;
        
        if (isNearBottom) {
            scrollBtn.classList.add('opacity-0', 'pointer-events-none');
        } else {
            scrollBtn.classList.remove('opacity-0', 'pointer-events-none');
        }
    });
}

// Event listeners
function initEventListeners() {
    // Message form
    document.getElementById('message-form').addEventListener('submit', (e) => {
        e.preventDefault();
        sendMessage();
    });

    // Enter to send (Shift+Enter for new line)
    document.getElementById('message-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // Sidebar toggle
    document.getElementById('sidebar-toggle').addEventListener('click', toggleSidebar);

    // User menu
    document.getElementById('user-menu-btn').addEventListener('click', () => {
        document.getElementById('user-menu-dropdown').classList.toggle('hidden');
    });

    // Close dropdowns on outside click
    document.addEventListener('click', (e) => {
        if (!e.target.closest('#user-menu-btn') && !e.target.closest('#user-menu-dropdown')) {
            document.getElementById('user-menu-dropdown').classList.add('hidden');
        }
        if (!e.target.closest('#emoji-picker') && !e.target.closest('[onclick="toggleEmojiPicker()"]')) {
            document.getElementById('emoji-picker').classList.add('hidden');
        }
        if (!e.target.closest('#context-menu')) {
            document.getElementById('context-menu').classList.add('hidden');
        }
    });

    // Search users
    document.getElementById('search-users').addEventListener('input', (e) => {
        filterUsers(e.target.value);
    });

    // Change password form
    document.getElementById('change-password-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData(e.target);
        try {
            const response = await fetch('/change_password', {
                method: 'POST',
                body: formData
            });
            const result = await response.json();
            if (result.success) {
                Toast.show('Password updated successfully!', 'success');
                e.target.reset();
            } else {
                Toast.show(result.error || 'Failed to update password', 'error');
            }
        } catch (error) {
            Toast.show('An error occurred', 'error');
        }
    });
}

// Load emojis
function loadEmojis() {
    const container = document.querySelector('#emoji-picker .emoji-grid');
    container.innerHTML = emojis.map(emoji => 
        `<button type="button" onclick="insertEmoji('${emoji}')" class="w-8 h-8 flex items-center justify-center hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg text-xl transition-colors">${emoji}</button>`
    ).join('');
}

// Toggle emoji picker
function toggleEmojiPicker() {
    document.getElementById('emoji-picker').classList.toggle('hidden');
}

// Insert emoji
function insertEmoji(emoji) {
    const input = document.getElementById('message-input');
    const start = input.selectionStart;
    const end = input.selectionEnd;
    const text = input.value;
    input.value = text.substring(0, start) + emoji + text.substring(end);
    input.focus();
    input.selectionStart = input.selectionEnd = start + emoji.length;
    toggleEmojiPicker();
}

// Toggle sidebar
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    sidebar.classList.toggle('-translate-x-full');
    overlay.classList.toggle('hidden');
}

// Switch tabs
function switchTab(tab) {
    const chatsTab = document.getElementById('tab-chats');
    const usersTab = document.getElementById('tab-users');
    
    if (tab === 'chats') {
        chatsTab.classList.add('text-primary-500', 'border-primary-500');
        chatsTab.classList.remove('text-gray-500', 'border-transparent');
        usersTab.classList.remove('text-primary-500', 'border-primary-500');
        usersTab.classList.add('text-gray-500', 'border-transparent');
    } else {
        usersTab.classList.add('text-primary-500', 'border-primary-500');
        usersTab.classList.remove('text-gray-500', 'border-transparent');
        chatsTab.classList.remove('text-primary-500', 'border-primary-500');
        chatsTab.classList.add('text-gray-500', 'border-transparent');
    }
}

// Join room
function joinRoom(room) {
    if (state.currentRoom !== room) {
        state.socket.emit('leave', { room: state.currentRoom });
    }
    
    state.currentRoom = room;
    state.socket.emit('join', { room });
    
    // Update UI
    document.querySelectorAll('.user-item').forEach(item => {
        item.classList.remove('active');
    });
    
    if (room === 'general') {
        document.getElementById('general-chat-item').classList.add('active');
        updateChatHeader('General', 'Public chat room', null, true);
    }
    
    // Clear messages
    document.getElementById('messages').innerHTML = '';
    
    // Close sidebar on mobile
    if (window.innerWidth < 1024) {
        toggleSidebar();
    }
}

// Start private chat
function startPrivateChat(targetUser) {
    const room = [state.currentUser, targetUser].sort().join(':');
    
    if (state.currentRoom !== room) {
        state.socket.emit('leave', { room: state.currentRoom });
    }
    
    state.currentRoom = room;
    state.socket.emit('start_private_chat', { target_user: targetUser });
    
    // Update UI
    document.querySelectorAll('.user-item').forEach(item => {
        item.classList.remove('active');
    });
    
    const userItem = document.querySelector(`[data-username="${targetUser}"]`);
    if (userItem) {
        userItem.classList.add('active');
        const avatar = userItem.querySelector('img');
        const avatarSrc = avatar ? avatar.src : null;
        updateChatHeader(targetUser, 'Private conversation', avatarSrc, false);
    }
    
    // Clear messages
    document.getElementById('messages').innerHTML = '';
    
    // Close sidebar on mobile
    if (window.innerWidth < 1024) {
        toggleSidebar();
    }
}

// Update chat header
function updateChatHeader(name, status, avatarSrc, isGeneral) {
    document.getElementById('chat-name').textContent = name;
    document.getElementById('chat-status').textContent = status;
    
    const avatar = document.getElementById('chat-avatar');
    if (isGeneral) {
        avatar.innerHTML = '<i data-lucide="hash" class="w-5 h-5"></i>';
        avatar.className = 'w-10 h-10 rounded-full bg-gradient-to-br from-primary-400 to-purple-500 flex items-center justify-center text-white';
    } else if (avatarSrc) {
        avatar.innerHTML = `<img src="${avatarSrc}" alt="${name}" class="w-full h-full rounded-full object-cover">`;
        avatar.className = 'w-10 h-10 rounded-full overflow-hidden';
    } else {
        avatar.innerHTML = `<span class="font-semibold">${name[0].toUpperCase()}</span>`;
        avatar.className = 'w-10 h-10 rounded-full bg-gradient-to-br from-gray-400 to-gray-500 flex items-center justify-center text-white';
    }
    
    lucide.createIcons();
}

// Load messages
function loadMessages(messages) {
    const container = document.getElementById('messages');
    container.innerHTML = '';
    
    messages.forEach(msg => addMessage(msg, false));
    scrollToBottom();
}

// Add message
function addMessage(data, animate = true) {
    const container = document.getElementById('messages');
    const isSent = data.username === state.currentUser;
    
    const messageEl = document.createElement('div');
    messageEl.id = `message-${data._id}`;
    messageEl.className = `flex ${isSent ? 'justify-end' : 'justify-start'} ${animate ? 'animate-slide-up' : ''}`;
    
    // Build attachments HTML
    let attachmentsHtml = '';
    const attachments = data.attachments || (data.attachment ? [data.attachment] : []);
    
    if (attachments.length > 0) {
        attachmentsHtml = '<div class="mt-2 space-y-2">';
        attachments.forEach(att => {
            const fileType = att.file_type || 'file';
            if (fileType === 'image') {
                attachmentsHtml += `
                    <div class="image-container relative cursor-pointer" onclick="openImageModal('${att.file_url}')">
                        <img src="${att.file_url}" alt="${att.file_name || 'Image'}" class="attachment-preview rounded-lg">
                        <div class="image-overlay absolute inset-0 bg-black/50 rounded-lg flex items-center justify-center">
                            <i data-lucide="zoom-in" class="w-6 h-6 text-white"></i>
                        </div>
                    </div>
                `;
            } else if (fileType === 'video') {
                attachmentsHtml += `
                    <video src="${att.file_url}" controls class="attachment-preview rounded-lg max-w-full"></video>
                `;
            } else if (fileType === 'audio') {
                attachmentsHtml += `
                    <audio src="${att.file_url}" controls class="w-full"></audio>
                `;
            } else {
                attachmentsHtml += `
                    <a href="${att.file_url}" target="_blank" class="flex items-center gap-3 p-3 bg-white/10 dark:bg-black/10 rounded-lg hover:bg-white/20 dark:hover:bg-black/20 transition-colors">
                        <i data-lucide="file" class="w-8 h-8 ${isSent ? 'text-white/70' : 'text-gray-400'}"></i>
                        <div class="flex-1 min-w-0">
                            <p class="text-sm font-medium truncate ${isSent ? 'text-white' : 'text-gray-900 dark:text-white'}">${att.file_name || 'File'}</p>
                            <p class="text-xs ${isSent ? 'text-white/70' : 'text-gray-500'}">${formatFileSize(att.file_size)}</p>
                        </div>
                        <i data-lucide="download" class="w-5 h-5 ${isSent ? 'text-white/70' : 'text-gray-400'}"></i>
                    </a>
                `;
            }
        });
        attachmentsHtml += '</div>';
    }
    
    // Format time
    const time = formatTime(data.timestamp);
    const editedBadge = data.edited ? '<span class="text-xs opacity-70 ml-1">(edited)</span>' : '';
    
    messageEl.innerHTML = `
        <div 
            class="message-bubble ${isSent ? 'message-sent text-white' : 'message-received text-gray-900 dark:text-white'} px-4 py-3 rounded-2xl ${isSent ? 'rounded-br-md' : 'rounded-bl-md'} shadow-sm"
            oncontextmenu="showContextMenu(event, '${data._id}', ${JSON.stringify(data).replace(/"/g, '&quot;')})"
        >
            ${!isSent ? `<p class="text-xs font-semibold text-primary-500 mb-1">${data.username}</p>` : ''}
            ${data.message ? `<p class="whitespace-pre-wrap break-words">${escapeHtml(data.message)}</p>` : ''}
            ${attachmentsHtml}
            <p class="text-xs ${isSent ? 'text-white/70' : 'text-gray-500 dark:text-gray-400'} mt-1 text-right">
                ${time}${editedBadge}
            </p>
        </div>
    `;
    
    container.appendChild(messageEl);
    lucide.createIcons();
    
    // Auto scroll if near bottom
    const messagesContainer = document.getElementById('messages-container');
    const isNearBottom = messagesContainer.scrollHeight - messagesContainer.scrollTop - messagesContainer.clientHeight < 200;
    if (isNearBottom || isSent) {
        scrollToBottom();
    }
}

// Add status message
function addStatusMessage(message) {
    const container = document.getElementById('messages');
    const statusEl = document.createElement('div');
    statusEl.className = 'flex justify-center my-4';
    statusEl.innerHTML = `
        <span class="px-4 py-1.5 bg-gray-200/50 dark:bg-dark-800/50 text-gray-500 dark:text-gray-400 text-xs rounded-full">
            ${escapeHtml(message)}
        </span>
    `;
    container.appendChild(statusEl);
    scrollToBottom();
}

// Remove message
function removeMessage(messageId) {
    const messageEl = document.getElementById(`message-${messageId}`);
    if (messageEl) {
        gsap.to(messageEl, {
            opacity: 0,
            height: 0,
            duration: 0.3,
            onComplete: () => messageEl.remove()
        });
    }
}

// Update message
function updateMessage(data) {
    const messageEl = document.getElementById(`message-${data.message_id}`);
    if (messageEl) {
        const textEl = messageEl.querySelector('p.whitespace-pre-wrap');
        if (textEl) {
            textEl.textContent = data.new_message;
        }
        // Add edited badge if not present
        const timeEl = messageEl.querySelector('.text-right');
        if (timeEl && !timeEl.innerHTML.includes('(edited)')) {
            timeEl.innerHTML += '<span class="text-xs opacity-70 ml-1">(edited)</span>';
        }
    }
}

// Send message
async function sendMessage() {
    const input = document.getElementById('message-input');
    const message = input.value.trim();
    
    if (!message && state.attachments.length === 0) return;
    
    const messageData = {
        room: state.currentRoom,
        message: message
    };
    
    if (state.attachments.length > 0) {
        messageData.attachments = state.attachments;
    }
    
    if (state.replyTo) {
        messageData.reply_to = state.replyTo;
    }
    
    state.socket.emit('message', messageData);
    
    // Clear input and state
    input.value = '';
    input.style.height = 'auto';
    state.attachments = [];
    state.replyTo = null;
    document.getElementById('attachment-preview').classList.add('hidden');
    document.getElementById('attachment-preview').innerHTML = '';
    cancelReply();
    
    // Stop typing indicator
    if (state.isTyping) {
        state.socket.emit('typing_stop', { room: state.currentRoom });
        state.isTyping = false;
    }
}

// Handle typing
function handleTyping() {
    if (!state.isTyping) {
        state.isTyping = true;
        state.socket.emit('typing_start', { room: state.currentRoom });
    }
    
    clearTimeout(state.typingTimeout);
    state.typingTimeout = setTimeout(() => {
        state.socket.emit('typing_stop', { room: state.currentRoom });
        state.isTyping = false;
    }, 2000);
}

// Handle typing indicator
function handleTypingIndicator(data) {
    if (data.room !== state.currentRoom || data.username === state.currentUser) return;
    
    const indicator = document.getElementById('typing-indicator');
    const userEl = document.getElementById('typing-user');
    
    if (data.typing) {
        userEl.textContent = `${data.username} is typing`;
        indicator.classList.remove('hidden');
        indicator.classList.add('flex');
    } else {
        indicator.classList.add('hidden');
        indicator.classList.remove('flex');
    }
}

// Handle attachments
async function handleAttachments(files) {
    if (!files || files.length === 0) return;
    
    const maxFiles = 5 - state.attachments.length;
    const filesToUpload = Array.from(files).slice(0, maxFiles);
    
    for (const file of filesToUpload) {
        if (file.size > 5 * 1024 * 1024) {
            Toast.show(`File ${file.name} is too large (max 5MB)`, 'error');
            continue;
        }
        
        // Show preview
        addAttachmentPreview(file);
        
        // Upload file
        const formData = new FormData();
        formData.append('file', file);
        
        try {
            const response = await fetch('/upload_attachment', {
                method: 'POST',
                body: formData
            });
            
            const result = await response.json();
            
            if (result.success) {
                state.attachments.push({
                    file_url: result.file_url,
                    file_name: result.file_name,
                    file_size: result.file_size,
                    file_type: result.file_type
                });
            } else {
                Toast.show(result.error || 'Upload failed', 'error');
                removeAttachmentPreview(file.name);
            }
        } catch (error) {
            Toast.show('Upload failed', 'error');
            removeAttachmentPreview(file.name);
        }
    }
}

// Add attachment preview
function addAttachmentPreview(file) {
    const container = document.getElementById('attachment-preview');
    container.classList.remove('hidden');
    
    const preview = document.createElement('div');
    preview.className = 'relative';
    preview.dataset.filename = file.name;
    
    if (file.type.startsWith('image/')) {
        const reader = new FileReader();
        reader.onload = (e) => {
            preview.innerHTML = `
                <img src="${e.target.result}" alt="${file.name}" class="w-20 h-20 object-cover rounded-lg">
                <button onclick="removeAttachment('${file.name}')" class="absolute -top-2 -right-2 w-6 h-6 bg-red-500 text-white rounded-full flex items-center justify-center hover:bg-red-600">
                    <i data-lucide="x" class="w-3 h-3"></i>
                </button>
            `;
            lucide.createIcons();
        };
        reader.readAsDataURL(file);
    } else {
        preview.innerHTML = `
            <div class="w-20 h-20 bg-gray-100 dark:bg-dark-800 rounded-lg flex flex-col items-center justify-center">
                <i data-lucide="file" class="w-6 h-6 text-gray-400"></i>
                <span class="text-xs text-gray-500 mt-1 truncate w-full px-1 text-center">${file.name.slice(0, 10)}</span>
            </div>
            <button onclick="removeAttachment('${file.name}')" class="absolute -top-2 -right-2 w-6 h-6 bg-red-500 text-white rounded-full flex items-center justify-center hover:bg-red-600">
                <i data-lucide="x" class="w-3 h-3"></i>
            </button>
        `;
        lucide.createIcons();
    }
    
    container.appendChild(preview);
}

// Remove attachment
function removeAttachment(filename) {
    state.attachments = state.attachments.filter(att => att.file_name !== filename);
    removeAttachmentPreview(filename);
}

// Remove attachment preview
function removeAttachmentPreview(filename) {
    const container = document.getElementById('attachment-preview');
    const preview = container.querySelector(`[data-filename="${filename}"]`);
    if (preview) {
        preview.remove();
    }
    if (container.children.length === 0) {
        container.classList.add('hidden');
    }
}

// Context menu
function showContextMenu(event, messageId, messageData) {
    event.preventDefault();
    
    state.contextMessageId = messageId;
    state.contextMessageData = messageData;
    
    const menu = document.getElementById('context-menu');
    const editBtn = document.getElementById('edit-btn');
    const deleteBtn = document.getElementById('delete-btn');
    
    // Show edit/delete only for own messages
    if (messageData.username === state.currentUser) {
        editBtn.classList.remove('hidden');
        deleteBtn.classList.remove('hidden');
    } else {
        editBtn.classList.add('hidden');
        deleteBtn.classList.add('hidden');
    }
    
    // Position menu
    menu.style.left = `${event.clientX}px`;
    menu.style.top = `${event.clientY}px`;
    menu.classList.remove('hidden');
    
    // Ensure menu stays in viewport
    const rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) {
        menu.style.left = `${window.innerWidth - rect.width - 10}px`;
    }
    if (rect.bottom > window.innerHeight) {
        menu.style.top = `${window.innerHeight - rect.height - 10}px`;
    }
}

// Copy message
function copyMessage() {
    if (state.contextMessageData) {
        navigator.clipboard.writeText(state.contextMessageData.message || '');
        Toast.show('Copied to clipboard', 'success');
    }
    document.getElementById('context-menu').classList.add('hidden');
}

// Reply to message
function replyToMessage() {
    if (state.contextMessageData) {
        state.replyTo = {
            _id: state.contextMessageId,
            username: state.contextMessageData.username,
            message: state.contextMessageData.message
        };
        
        document.getElementById('reply-preview').classList.remove('hidden');
        document.getElementById('reply-username').textContent = state.contextMessageData.username;
        document.getElementById('reply-message').textContent = state.contextMessageData.message || '[Attachment]';
        document.getElementById('message-input').focus();
    }
    document.getElementById('context-menu').classList.add('hidden');
}

// Cancel reply
function cancelReply() {
    state.replyTo = null;
    document.getElementById('reply-preview').classList.add('hidden');
}

// Edit message
function editMessage() {
    if (state.contextMessageId && state.contextMessageData) {
        const newMessage = prompt('Edit message:', state.contextMessageData.message);
        if (newMessage !== null && newMessage.trim() !== '') {
            state.socket.emit('edit_message', {
                message_id: state.contextMessageId,
                room: state.currentRoom,
                new_message: newMessage.trim()
            });
        }
    }
    document.getElementById('context-menu').classList.add('hidden');
}

// Delete message
function deleteMessage() {
    if (state.contextMessageId && confirm('Delete this message?')) {
        state.socket.emit('delete_message', {
            message_id: state.contextMessageId,
            room: state.currentRoom
        });
    }
    document.getElementById('context-menu').classList.add('hidden');
}

// Update users list
function updateUsersList(users) {
    users.forEach(user => {
        if (user.username === state.currentUser) return;
        
        const userItem = document.querySelector(`[data-username="${user.username}"]`);
        if (userItem) {
            const statusIndicator = userItem.querySelector('.status-indicator');
            if (statusIndicator) {
                statusIndicator.className = `status-indicator absolute bottom-0 right-0 w-3.5 h-3.5 rounded-full border-2 border-white dark:border-dark-900 ${user.online ? 'bg-green-500 online-pulse' : 'bg-gray-400'}`;
            }
            
            const statusText = userItem.querySelector('p.text-sm');
            if (statusText) {
                if (user.online) {
                    statusText.innerHTML = '<span class="text-green-500">Online</span>';
                } else {
                    statusText.textContent = `Last seen: ${user.last_seen ? user.last_seen.slice(0, 10) : 'Unknown'}`;
                }
            }
        }
    });
}

// Update user status
function updateUserStatus(username, isOnline) {
    const userItem = document.querySelector(`[data-username="${username}"]`);
    if (userItem) {
        const statusIndicator = userItem.querySelector('.status-indicator');
        if (statusIndicator) {
            statusIndicator.className = `status-indicator absolute bottom-0 right-0 w-3.5 h-3.5 rounded-full border-2 border-white dark:border-dark-900 ${isOnline ? 'bg-green-500 online-pulse' : 'bg-gray-400'}`;
        }
    }
}

// Handle notification
function handleNotification(data) {
    if (data.room !== state.currentRoom) {
        state.unreadCounts[data.room] = (state.unreadCounts[data.room] || 0) + 1;
        updateNotificationBadge();
        
        // Show toast
        Toast.show(`New message from ${data.sender}`, 'info');
        
        // Browser notification
        if (Notification.permission === 'granted') {
            new Notification('Chatlet', {
                body: `${data.sender}: ${data.message}`,
                icon: '/favicon.ico'
            });
        }
    }
}

// Update notification badge
function updateNotificationBadge() {
    const totalUnread = Object.values(state.unreadCounts).reduce((a, b) => a + b, 0);
    const badge = document.getElementById('notification-badge');
    
    if (totalUnread > 0) {
        badge.textContent = totalUnread > 99 ? '99+' : totalUnread;
        badge.classList.remove('hidden');
        badge.classList.add('badge-bounce');
        setTimeout(() => badge.classList.remove('badge-bounce'), 500);
    } else {
        badge.classList.add('hidden');
    }
}

// Update badge
function updateBadge(count) {
    // Update specific room badge if needed
}

// Filter users
function filterUsers(query) {
    const normalizedQuery = query.toLowerCase();
    const userItems = document.querySelectorAll('#users-list .user-item');
    
    userItems.forEach(item => {
        const username = item.dataset.username.toLowerCase();
        if (username.includes(normalizedQuery)) {
            item.style.display = '';
        } else {
            item.style.display = 'none';
        }
    });
}

// Settings modal
function openSettingsModal() {
    document.getElementById('settings-modal').classList.remove('hidden');
    document.getElementById('user-menu-dropdown').classList.add('hidden');
}

function closeSettingsModal() {
    document.getElementById('settings-modal').classList.add('hidden');
}

// Chat settings
function openChatSettings() {
    openSettingsModal();
}

// Set background
async function setBackground(type) {
    const formData = new FormData();
    formData.append('background_type', type);
    formData.append('background_value', type);
    
    try {
        const response = await fetch('/change_background', {
            method: 'POST',
            body: formData
        });
        
        const result = await response.json();
        if (result.success) {
            Toast.show('Background updated!', 'success');
            // Apply background
            const container = document.getElementById('messages-container');
            if (type === 'default') {
                container.style.backgroundImage = '';
            } else if (type === 'gradient1') {
                container.style.background = 'linear-gradient(135deg, rgba(99, 102, 241, 0.1) 0%, rgba(139, 92, 246, 0.1) 100%)';
            } else if (type === 'gradient2') {
                container.style.background = 'linear-gradient(135deg, rgba(34, 197, 94, 0.1) 0%, rgba(59, 130, 246, 0.1) 100%)';
            }
        } else {
            Toast.show(result.error || 'Failed to update background', 'error');
        }
    } catch (error) {
        Toast.show('An error occurred', 'error');
    }
}

// Upload background
async function uploadBackground(file) {
    if (!file) return;
    
    const formData = new FormData();
    formData.append('background_type', 'upload');
    formData.append('background_image', file);
    
    try {
        const response = await fetch('/change_background', {
            method: 'POST',
            body: formData
        });
        
        const result = await response.json();
        if (result.success) {
            Toast.show('Background updated!', 'success');
            document.getElementById('messages-container').style.backgroundImage = `url('${result.background_value}')`;
            document.getElementById('messages-container').style.backgroundSize = 'cover';
            document.getElementById('messages-container').style.backgroundPosition = 'center';
        } else {
            Toast.show(result.error || 'Failed to upload background', 'error');
        }
    } catch (error) {
        Toast.show('An error occurred', 'error');
    }
}

// Image modal
function openImageModal(src) {
    document.getElementById('modal-image').src = src;
    document.getElementById('modal-download').href = src;
    document.getElementById('image-modal').classList.remove('hidden');
}

function closeImageModal() {
    document.getElementById('image-modal').classList.add('hidden');
}

// Utility functions
function scrollToBottom() {
    const container = document.getElementById('messages-container');
    container.scrollTop = container.scrollHeight;
}

function formatTime(timestamp) {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: true });
}

function formatFileSize(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) {
        bytes /= 1024;
        i++;
    }
    return `${bytes.toFixed(1)} ${units[i]}`;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function playNotificationSound() {
    // Optional: Add notification sound
}

// Request notification permission
if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
}
