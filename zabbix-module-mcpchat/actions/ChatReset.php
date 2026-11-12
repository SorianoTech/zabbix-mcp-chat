<?php declare(strict_types = 0);

namespace Modules\McpChat\Actions;

/**
 * POST zabbix.php?action=mcpchat.reset  (JSON: {"_csrf_token": "..."})
 *
 * Borra en la pasarela el historial de la conversación del usuario.
 */
class ChatReset extends ChatBase {

	protected function init(): void {
		$this->setPostContentType(self::POST_CONTENT_TYPE_JSON);
	}

	protected function checkInput(): bool {
		return true;
	}

	protected function checkPermissions(): bool {
		return $this->isChatAllowed();
	}

	protected function doAction(): void {
		$result = $this->callGateway('/reset', ['conversation' => $this->getConversationId()]);
		$error = $this->gatewayError($result);

		$this->respondJson($error !== null ? ['error' => $error] : ['ok' => true]);
	}
}
