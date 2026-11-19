<?php declare(strict_types = 0);

namespace Modules\McpChat\Actions;

/**
 * POST zabbix.php?action=mcpchat.send  (JSON: {"message": "...", "_csrf_token": "..."})
 *
 * Reenvía la pregunta a la pasarela y devuelve {"answer", "tools_used", "usage", "new_conversation"}
 * o {"error"}. "new_conversation" = el asistente respondió sin recordar mensajes anteriores.
 */
class ChatSend extends ChatBase {

	private const MAX_MESSAGE_CHARS = 4000;

	protected function init(): void {
		$this->setPostContentType(self::POST_CONTENT_TYPE_JSON);
	}

	protected function checkInput(): bool {
		$ret = $this->validateInput([
			'message' => 'required|string|not_empty'
		]);

		if ($ret && mb_strlen($this->getInput('message')) > self::MAX_MESSAGE_CHARS) {
			$this->respondJson(['error' => 'El mensaje es demasiado largo (máx. '.self::MAX_MESSAGE_CHARS.' caracteres).']);

			return false;
		}

		if (!$ret) {
			$this->respondJson(['error' => 'Mensaje no válido.']);
		}

		return $ret;
	}

	protected function checkPermissions(): bool {
		return $this->isChatAllowed();
	}

	protected function doAction(): void {
		$payload = [
			'conversation' => $this->getConversationId(),
			'user' => $this->getUsername(),
			'message' => trim($this->getInput('message'))
		];

		// Delegación: las herramientas consultan Zabbix con la sesión de ESTE usuario, así que
		// Zabbix aplica sus permisos de hosts, su rol y su auditoría. Viaja solo servidor a servidor.
		if ($this->getModuleConfig()['delegate_user_session']) {
			$payload['zabbix_auth'] = $this->getUserSessionId();
		}

		$result = $this->callGateway('/chat', $payload);

		$error = $this->gatewayError($result);
		if ($error !== null) {
			$this->respondJson(['error' => $error]);

			return;
		}

		$this->respondJson([
			'answer' => (string) ($result['body']['answer'] ?? ''),
			'tools_used' => array_values(array_map('strval', (array) ($result['body']['tools_used'] ?? []))),
			'usage' => (array) ($result['body']['usage'] ?? []),
			'new_conversation' => (bool) ($result['body']['new_conversation'] ?? false)
		]);
	}
}
