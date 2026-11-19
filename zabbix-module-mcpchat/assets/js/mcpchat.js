/*
 * MCP Chat para Zabbix 7.0: globo de chat flotante en todas las páginas.
 * Habla solo con zabbix.php?action=mcpchat.* (mismo origen); la pasarela y el LLM quedan detrás.
 */
(function () {
	'use strict';

	if (window.top !== window.self || window.__mcpchatLoaded) {
		return; // ni en iframes ni dos veces
	}
	window.__mcpchatLoaded = true;

	const ENDPOINT = 'zabbix.php?action=';
	const MAX_STORED_MESSAGES = 60;

	let status = null;
	let storageKey = null;
	let messages = [];
	let pending = false;
	let els = {};
	let menuItems = [];   // comandos visibles en el menú de "/"
	let menuIndex = 0;

	// ---------------------------------------------------------------- utilidades

	function storageGet(key, fallback) {
		try {
			const raw = window.sessionStorage.getItem(key);
			return raw === null ? fallback : JSON.parse(raw);
		}
		catch (e) {
			return fallback;
		}
	}

	function storageSet(key, value) {
		try {
			window.sessionStorage.setItem(key, JSON.stringify(value));
		}
		catch (e) {
			// almacenamiento no disponible: el chat funciona igual, sin persistir entre páginas
		}
	}

	function escapeHtml(text) {
		return String(text)
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;')
			.replace(/'/g, '&#39;');
	}

	// Markdown mínimo y seguro: se escapa TODO primero y luego se aplican formatos simples.
	function inlineMarkdown(text) {
		return text
			.replace(/`([^`]+)`/g, '<code>$1</code>')
			.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
			.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
	}

	function renderMarkdown(source) {
		const lines = escapeHtml(source).split('\n');
		const out = [];
		let i = 0;

		while (i < lines.length) {
			const line = lines[i];

			if (/^```/.test(line.trim())) {
				const code = [];
				i++;
				while (i < lines.length && !/^```/.test(lines[i].trim())) {
					code.push(lines[i++]);
				}
				i++;
				out.push('<pre><code>' + code.join('\n') + '</code></pre>');
				continue;
			}

			if (/^\s*\|.*\|\s*$/.test(line)) {
				const rows = [];
				while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
					rows.push(lines[i++]);
				}
				const cells = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
				const body = rows.filter((row) => !/^\s*\|[\s:|-]+\|\s*$/.test(row));
				let html = '<div class="mcpchat-table"><table>';
				body.forEach((row, idx) => {
					const tag = idx === 0 ? 'th' : 'td';
					html += '<tr>' + cells(row).map((c) => '<' + tag + '>' + inlineMarkdown(c) + '</' + tag + '>')
						.join('') + '</tr>';
				});
				out.push(html + '</table></div>');
				continue;
			}

			if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
				const ordered = /^\s*\d+\./.test(line);
				const itemRe = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/;
				const items = [];
				while (i < lines.length && itemRe.test(lines[i])) {
					items.push('<li>' + inlineMarkdown(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, '')) + '</li>');
				}
				out.push((ordered ? '<ol>' : '<ul>') + items.join('') + (ordered ? '</ol>' : '</ul>'));
				continue;
			}

			const heading = line.match(/^#{1,6}\s+(.*)$/);
			if (heading) {
				out.push('<p class="mcpchat-h">' + inlineMarkdown(heading[1]) + '</p>');
			}
			else if (line.trim() !== '') {
				out.push('<p>' + inlineMarkdown(line) + '</p>');
			}
			i++;
		}

		return out.join('');
	}

	// Los errores del módulo son texto; los de Zabbix (p. ej. acceso denegado) son {title, messages}.
	function errorText(error) {
		if (typeof error === 'string') {
			return error;
		}
		if (error && Array.isArray(error.messages) && error.messages.length) {
			return (error.title ? error.title + ': ' : '') + error.messages[0]
				+ ' Si tu sesión ha cambiado, recarga la página.';
		}
		return 'Error desconocido.';
	}

	async function request(action, payload) {
		const options = {credentials: 'same-origin', headers: {'Accept': 'application/json'}};
		if (payload !== undefined) {
			options.method = 'POST';
			options.headers['Content-Type'] = 'application/json';
			options.body = JSON.stringify(payload);
		}
		const response = await fetch(ENDPOINT + action, options);
		const type = response.headers.get('Content-Type') || '';
		if (!type.includes('application/json')) {
			throw new Error('session');
		}
		return response.json();
	}

	// ---------------------------------------------------------------- interfaz

	const ICON_CHAT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 4v-4H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/></svg>';
	const ICON_CLOSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
	const ICON_RESET = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12a8 8 0 1 0 2.3-5.7M4 4v4h4"/></svg>';
	const ICON_SEND = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 20l18-8L3 4v6l12 2-12 2z"/></svg>';

	function build() {
		const root = document.createElement('div');
		root.className = 'mcpchat';
		root.innerHTML =
			'<button type="button" class="mcpchat-bubble" aria-label="Abrir asistente">' + ICON_CHAT + '</button>' +
			'<section class="mcpchat-panel" role="dialog" aria-label="Asistente" hidden>' +
				'<header class="mcpchat-header">' +
					'<span class="mcpchat-title"></span>' +
					'<button type="button" class="mcpchat-icon-btn mcpchat-reset" title="Nueva conversación"' +
						' aria-label="Nueva conversación">' + ICON_RESET + '</button>' +
					'<button type="button" class="mcpchat-icon-btn mcpchat-close" title="Cerrar"' +
						' aria-label="Cerrar">' + ICON_CLOSE + '</button>' +
				'</header>' +
				'<div class="mcpchat-messages" aria-live="polite"></div>' +
				'<form class="mcpchat-form">' +
					'<ul class="mcpchat-cmds" id="mcpchat-cmds" role="listbox" aria-label="Comandos" hidden></ul>' +
					'<textarea class="mcpchat-input" rows="2" maxlength="4000"' +
						' aria-autocomplete="list" aria-controls="mcpchat-cmds" aria-expanded="false"' +
						' placeholder="Pregunta sobre tu monitorización… (Enter envía, Mayús+Enter salto de línea)"></textarea>' +
					'<button type="submit" class="mcpchat-send" aria-label="Enviar">' + ICON_SEND + '</button>' +
				'</form>' +
			'</section>';
		document.body.appendChild(root);

		els = {
			root: root,
			bubble: root.querySelector('.mcpchat-bubble'),
			panel: root.querySelector('.mcpchat-panel'),
			title: root.querySelector('.mcpchat-title'),
			list: root.querySelector('.mcpchat-messages'),
			form: root.querySelector('.mcpchat-form'),
			input: root.querySelector('.mcpchat-input'),
			menu: root.querySelector('.mcpchat-cmds'),
			send: root.querySelector('.mcpchat-send'),
			reset: root.querySelector('.mcpchat-reset'),
			close: root.querySelector('.mcpchat-close')
		};
		els.title.textContent = status.title || 'Asistente Zabbix';

		els.bubble.addEventListener('click', () => setOpen(els.panel.hidden));
		els.close.addEventListener('click', () => setOpen(false));
		els.reset.addEventListener('click', resetConversation);
		els.form.addEventListener('submit', (e) => {
			e.preventDefault();
			send();
		});
		els.input.addEventListener('input', () => {
			menuIndex = 0;
			updateCommandMenu();
		});
		els.input.addEventListener('blur', hideCommandMenu);
		els.input.addEventListener('keydown', (e) => {
			if (!els.menu.hidden && !e.isComposing && handleMenuKey(e)) {
				e.preventDefault();
				return;
			}
			if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
				e.preventDefault();
				send();
			}
			else if (e.key === 'Escape') {
				setOpen(false);
			}
		});
	}

	function setOpen(open) {
		els.panel.hidden = !open;
		els.bubble.classList.toggle('mcpchat-bubble-active', open);
		storageSet(storageKey + ':open', open);
		if (open) {
			scrollToEnd();
			els.input.focus();
		}
	}

	function scrollToEnd() {
		els.list.scrollTop = els.list.scrollHeight;
	}

	function renderMessage(msg) {
		const div = document.createElement('div');
		div.className = 'mcpchat-msg mcpchat-' + msg.role;
		if (msg.role === 'assistant') {
			div.innerHTML = renderMarkdown(msg.text);
			if (msg.tools && msg.tools.length) {
				const tools = document.createElement('div');
				tools.className = 'mcpchat-tools';
				tools.textContent = 'Consultado: ' + Array.from(new Set(msg.tools)).join(', ');
				div.appendChild(tools);
			}
		}
		else {
			div.textContent = msg.text;
		}
		els.list.appendChild(div);
	}

	function renderAll() {
		els.list.innerHTML = '';
		if (!messages.length) {
			renderMessage({
				role: 'info',
				text: 'Hola. Pregúntame por problemas, hosts, ítems o tendencias de Zabbix. Escribe / para ver los comandos.'
			});
		}
		messages.forEach(renderMessage);
		scrollToEnd();
	}

	function addMessage(msg) {
		messages.push(msg);
		messages = messages.slice(-MAX_STORED_MESSAGES);
		storageSet(storageKey + ':messages', messages);
		if (messages.length === 1) {
			renderAll();
		}
		else {
			renderMessage(msg);
			scrollToEnd();
		}
	}

	function setPending(value) {
		pending = value;
		els.input.disabled = value;
		els.send.disabled = value;
		els.reset.disabled = value;
		const typing = els.list.querySelector('.mcpchat-typing');
		if (value && !typing) {
			const div = document.createElement('div');
			div.className = 'mcpchat-msg mcpchat-assistant mcpchat-typing';
			div.innerHTML = '<span></span><span></span><span></span>';
			els.list.appendChild(div);
			scrollToEnd();
		}
		else if (!value && typing) {
			typing.remove();
		}
	}

	// ---------------------------------------------------------------- comandos
	// Se resuelven en el navegador: no llegan a la pasarela ni al LLM (no gastan tokens).

	const COMMANDS = [
		{name: 'nueva', aliases: ['reset'], help: 'Empieza una conversación nueva: el asistente olvida la actual.',
			run: () => resetConversation()},
		{name: 'limpiar', aliases: [], help: 'Borra los mensajes de la pantalla; el asistente sigue recordándolos.',
			run: () => clearScreen()},
		{name: 'ayuda', aliases: ['help'], help: 'Muestra los comandos disponibles.', run: () => showHelp()}
	];
	// Una sola palabra tras "/": "/var/log/zabbix está lleno" o "/ruta algo" se envían como pregunta.
	const COMMAND_RE = /^\/([a-zñ]*)$/i;

	function findCommand(name) {
		return COMMANDS.find((c) => c.name === name || c.aliases.includes(name));
	}

	// Devuelve true si el texto era un comando (y ya se ha ejecutado o rechazado).
	function runCommand(text) {
		const match = text.match(COMMAND_RE);
		if (!match) {
			return false;
		}
		const name = match[1].toLowerCase();
		const command = findCommand(name === '' ? 'ayuda' : name);
		els.input.value = '';
		hideCommandMenu();
		if (command) {
			command.run();
		}
		else {
			addMessage({role: 'info', text: 'Comando no reconocido: /' + name + '. Escribe /ayuda para ver los disponibles.'});
		}
		return true;
	}

	function showHelp() {
		addMessage({
			role: 'help',
			text: 'Comandos (no se envían al asistente):\n' + COMMANDS.map((c) =>
				'/' + c.name + (c.aliases.length ? ' (o /' + c.aliases.join(', /') + ')' : '') + ': ' + c.help
			).join('\n')
		});
	}

	function clearScreen() {
		messages = [];
		storageSet(storageKey + ':messages', messages);
		renderAll();
	}

	function updateCommandMenu() {
		const match = els.input.value.match(COMMAND_RE);
		const prefix = match ? match[1].toLowerCase() : null;
		menuItems = (prefix === null || pending) ? [] : COMMANDS.filter(
			(c) => [c.name].concat(c.aliases).some((n) => n.startsWith(prefix))
		);
		if (!menuItems.length) {
			hideCommandMenu();
			return;
		}
		menuIndex = Math.min(menuIndex, menuItems.length - 1);
		els.menu.innerHTML = '';
		menuItems.forEach((command, i) => {
			const li = document.createElement('li');
			li.id = 'mcpchat-cmd-' + i;
			li.className = 'mcpchat-cmd' + (i === menuIndex ? ' mcpchat-cmd-active' : '');
			li.setAttribute('role', 'option');
			li.setAttribute('aria-selected', String(i === menuIndex));
			const name = document.createElement('span');
			name.className = 'mcpchat-cmd-name';
			name.textContent = '/' + command.name;
			const help = document.createElement('span');
			help.className = 'mcpchat-cmd-help';
			help.textContent = command.help;
			li.append(name, help);
			// mousedown + preventDefault: el textarea no pierde el foco (su blur cerraría el menú).
			li.addEventListener('mousedown', (e) => {
				e.preventDefault();
				executeMenuItem(i);
			});
			els.menu.appendChild(li);
		});
		els.menu.hidden = false;
		els.input.setAttribute('aria-expanded', 'true');
		els.input.setAttribute('aria-activedescendant', 'mcpchat-cmd-' + menuIndex);
	}

	function hideCommandMenu() {
		els.menu.hidden = true;
		els.menu.innerHTML = '';
		menuItems = [];
		els.input.setAttribute('aria-expanded', 'false');
		els.input.removeAttribute('aria-activedescendant');
	}

	function executeMenuItem(i) {
		const command = menuItems[i];
		els.input.value = '';
		hideCommandMenu();
		if (command) {
			command.run();
		}
		els.input.focus();
	}

	// Teclas con el menú abierto. Devuelve true si la tecla se ha consumido.
	function handleMenuKey(e) {
		const n = menuItems.length;
		if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
			menuIndex = (menuIndex + (e.key === 'ArrowDown' ? 1 : n - 1)) % n;
			updateCommandMenu();
			return true;
		}
		if (e.key === 'Tab') {
			els.input.value = '/' + menuItems[menuIndex].name;
			updateCommandMenu();
			return true;
		}
		if (e.key === 'Enter' && !e.shiftKey) {
			executeMenuItem(menuIndex);
			return true;
		}
		if (e.key === 'Escape') {
			hideCommandMenu();
			return true;
		}
		return false;
	}

	// ---------------------------------------------------------------- acciones

	async function send() {
		const text = els.input.value.trim();
		if (!text || pending) {
			return;
		}
		if (runCommand(text)) {
			return;
		}
		els.input.value = '';
		addMessage({role: 'user', text: text});
		setPending(true);

		try {
			const data = await request('mcpchat.send', {message: text, _csrf_token: status.csrf.send});
			if (data.error) {
				addMessage({role: 'error', text: errorText(data.error)});
			}
			else {
				// La pasarela perdió la memoria (caducidad, reinicio, reset en otra pestaña) pero aquí aún
				// se ven respuestas anteriores: avisar de que el asistente ya no las recuerda.
				const earlier = messages.slice(0, -1).some((m) => m.role === 'assistant');
				if (data.new_conversation && earlier) {
					addMessage({
						role: 'info',
						text: 'La conversación anterior caducó: el asistente no recuerda los mensajes previos a esta pregunta.'
					});
				}
				addMessage({role: 'assistant', text: data.answer || '(sin respuesta)', tools: data.tools_used || []});
			}
		}
		catch (e) {
			addMessage({
				role: 'error',
				text: e.message === 'session'
					? 'Tu sesión de Zabbix ha caducado o el token ya no es válido. Recarga la página.'
					: 'No se pudo enviar el mensaje: ' + e.message
			});
		}
		finally {
			setPending(false);
			els.input.focus();
		}
	}

	async function resetConversation() {
		if (pending) {
			return;
		}
		if (messages.length && !window.confirm('¿Empezar una conversación nueva? El asistente olvidará la actual.')) {
			return;
		}
		try {
			const data = await request('mcpchat.reset', {_csrf_token: status.csrf.reset});
			if (data.error) {
				addMessage({role: 'error', text: errorText(data.error)});
				return;
			}
		}
		catch (e) {
			addMessage({role: 'error', text: 'No se pudo reiniciar la conversación. Recarga la página.'});
			return;
		}
		messages = [];
		storageSet(storageKey + ':messages', messages);
		renderAll();
	}

	// ---------------------------------------------------------------- arranque

	async function start() {
		try {
			status = await request('mcpchat.status');
		}
		catch (e) {
			return; // sin sesión (p. ej. página de login): no se muestra nada
		}
		if (!status || !status.allowed) {
			return;
		}
		storageKey = 'mcpchat:' + status.conversation;
		messages = storageGet(storageKey + ':messages', []);
		build();
		renderAll();
		if (storageGet(storageKey + ':open', false)) {
			setOpen(true);
		}
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', start);
	}
	else {
		start();
	}
})();
