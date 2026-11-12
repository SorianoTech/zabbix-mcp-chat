<?php declare(strict_types = 0);

namespace Modules\McpChat\Actions;

use CCsrfTokenHelper;

/**
 * GET zabbix.php?action=mcpchat.status
 *
 * Lo llama el JavaScript al cargar cada página: indica si el usuario puede usar el chat y le
 * entrega los tokens CSRF de las acciones que modifican estado.
 */
class ChatStatus extends ChatBase {

	protected function init(): void {
		$this->disableCsrfValidation();
	}

	protected function checkInput(): bool {
		return true;
	}

	protected function checkPermissions(): bool {
		// Siempre responde; el resultado dice si el chat está permitido.
		return true;
	}

	protected function doAction(): void {
		if (!$this->isChatAllowed()) {
			$this->respondJson(['allowed' => false]);

			return;
		}

		$this->respondJson([
			'allowed' => true,
			'title' => (string) $this->getModuleConfig()['title'],
			// Prefijo del id de conversación: separa el historial visible entre usuarios/sesiones.
			'conversation' => substr($this->getConversationId(), 0, 16),
			'csrf' => [
				'send' => CCsrfTokenHelper::get('mcpchat.send'),
				'reset' => CCsrfTokenHelper::get('mcpchat.reset')
			]
		]);
	}
}
