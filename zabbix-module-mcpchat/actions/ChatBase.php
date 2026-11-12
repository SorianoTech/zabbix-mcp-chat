<?php declare(strict_types = 0);

namespace Modules\McpChat\Actions;

use APP;
use CController;
use CControllerResponseData;
use CWebUser;

/**
 * Lógica común de las acciones del chat: configuración, permisos y llamada a la pasarela.
 */
abstract class ChatBase extends CController {

	private const DEFAULTS = [
		'gateway_url' => 'http://127.0.0.1:8770',
		'gateway_api_key' => '',
		'min_user_type' => USER_TYPE_SUPER_ADMIN,
		'timeout' => 180,
		'title' => 'Asistente Zabbix',
		'delegate_user_session' => true
	];

	protected function getModuleConfig(): array {
		$module = APP::ModuleManager()->getActionModule();
		$config = ($module !== null ? $module->getConfig() : []) + self::DEFAULTS;

		// Permite no guardar la clave en la base de datos de Zabbix: variable de entorno del servidor web.
		$env_key = getenv('MCPCHAT_GATEWAY_API_KEY');
		if ($env_key !== false && $env_key !== '') {
			$config['gateway_api_key'] = $env_key;
		}

		return $config;
	}

	protected function isChatAllowed(): bool {
		return CWebUser::isLoggedIn() && !CWebUser::isGuest()
			&& $this->getUserType() >= (int) $this->getModuleConfig()['min_user_type'];
	}

	/**
	 * Identificador de conversación: un usuario y una sesión de Zabbix = una conversación.
	 * Es un hash: el identificador de sesión real nunca sale del servidor web.
	 */
	protected function getConversationId(): string {
		return hash('sha256', CWebUser::$data['userid'].'|'.(CWebUser::$data['sessionid'] ?? ''));
	}

	/**
	 * Sesión de Zabbix del usuario conectado. En Zabbix 7.0 la API acepta este id como
	 * "Authorization: Bearer", y lo identifica como el propio usuario (mismos permisos).
	 */
	protected function getUserSessionId(): string {
		return (string) (CWebUser::$data['sessionid'] ?? '');
	}

	protected function getUsername(): string {
		return (string) (CWebUser::$data['username'] ?? CWebUser::$data['alias'] ?? CWebUser::$data['userid']);
	}

	protected function respondJson(array $data): void {
		$this->setResponse(new CControllerResponseData(['main_block' => json_encode($data)]));
	}

	/**
	 * POST JSON a la pasarela. Devuelve ['status' => int, 'body' => array|null].
	 * status 0 = no se pudo conectar.
	 */
	protected function callGateway(string $path, array $payload): array {
		$config = $this->getModuleConfig();
		$url = rtrim((string) $config['gateway_url'], '/').$path;
		$timeout = max(5, (int) $config['timeout']);
		$json = json_encode($payload);

		$headers = ['Content-Type: application/json', 'Accept: application/json'];
		if ($config['gateway_api_key'] !== '') {
			$headers[] = 'Authorization: Bearer '.$config['gateway_api_key'];
		}

		@set_time_limit($timeout + 15);

		if (function_exists('curl_init')) {
			$ch = curl_init($url);
			curl_setopt_array($ch, [
				CURLOPT_POST => true,
				CURLOPT_POSTFIELDS => $json,
				CURLOPT_HTTPHEADER => $headers,
				CURLOPT_RETURNTRANSFER => true,
				CURLOPT_CONNECTTIMEOUT => 5,
				CURLOPT_TIMEOUT => $timeout
			]);
			$raw = curl_exec($ch);
			$status = ($raw === false) ? 0 : (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
			if ($raw === false) {
				error_log('[mcpchat] Error conectando con la pasarela '.$url.': '.curl_error($ch));
			}
			curl_close($ch);
		}
		else {
			$context = stream_context_create(['http' => [
				'method' => 'POST',
				'header' => implode("\r\n", $headers),
				'content' => $json,
				'timeout' => $timeout,
				'ignore_errors' => true
			]]);
			$raw = @file_get_contents($url, false, $context);
			$status = 0;
			if ($raw !== false && isset($http_response_header[0])
					&& preg_match('/^HTTP\/\S+\s+(\d{3})/', $http_response_header[0], $m)) {
				$status = (int) $m[1];
			}
			if ($raw === false) {
				error_log('[mcpchat] Error conectando con la pasarela '.$url);
			}
		}

		$body = ($raw === false) ? null : json_decode($raw, true);

		return ['status' => $status, 'body' => is_array($body) ? $body : null];
	}

	/**
	 * Traduce el resultado de la pasarela a un mensaje de error para el usuario (o null si fue bien).
	 */
	protected function gatewayError(array $result): ?string {
		if ($result['status'] === 0) {
			return 'No se puede contactar con la pasarela del asistente. Avisa al administrador.';
		}
		if ($result['status'] === 401) {
			error_log('[mcpchat] La pasarela rechazó la clave (gateway_api_key)');
			return 'La pasarela del asistente rechazó la autenticación. Avisa al administrador.';
		}
		if ($result['status'] >= 400 || $result['body'] === null) {
			return $result['body']['error'] ?? 'Error inesperado de la pasarela (HTTP '.$result['status'].').';
		}

		return null;
	}
}
