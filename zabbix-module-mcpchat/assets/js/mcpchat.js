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
					'<textarea class="mcpchat-input" rows="2" maxlength="4000"' +
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
		els.input.addEventListener('keydown', (e) => {
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
			renderMessage({role: 'info', text: 'Hola. Pregúntame por problemas, hosts, ítems o tendencias de Zabbix.'});
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

	// ---------------------------------------------------------------- acciones

	async function send() {
		const text = els.input.value.trim();
		if (!text || pending) {
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
