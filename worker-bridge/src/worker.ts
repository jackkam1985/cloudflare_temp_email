/**
 * Cloudflare Email Relay Bridge Worker
 *
 * Deploy this Worker to the Cloudflare account that owns your domain.
 * It receives emails via Email Routing and forwards the raw content to the main
 * cloudflare_temp_email Worker running in a different account.
 *
 * Required environment variables / secrets:
 *   MAIN_WORKER_URL    - Full URL of the main Worker, e.g. https://my-worker.workers.dev
 *   MAIL_RELAY_SECRET  - Shared secret that authenticates this bridge to the main Worker
 *                        Must match MAIL_RELAY_SECRET set on the main Worker.
 *                        Set via: wrangler secret put MAIL_RELAY_SECRET
 *   MAIN_WORKER_PASSWORD - (Optional) Password for main Worker's x-custom-auth header
 *                          Only needed if main Worker has password protection enabled.
 */

interface Env {
    MAIN_WORKER_URL: string
    MAIL_RELAY_SECRET: string
    MAIN_WORKER_PASSWORD?: string
}

export default {
    async fetch(
        request: Request,
        env: Env,
        _ctx: ExecutionContext
    ): Promise<Response> {
        const url = new URL(request.url)

        if (url.pathname === '/health') {
            return new Response(
                JSON.stringify({ status: 'ok' }),
                {
                    status: 200,
                    headers: {
                        'Content-Type': 'application/json',
                    },
                }
            )
        }

        return new Response('Not Found', { status: 404 })
    },

    async email(
        message: ForwardableEmailMessage,
        env: Env,
        _ctx: ExecutionContext
    ): Promise<void> {
        if (!env.MAIN_WORKER_URL || !env.MAIL_RELAY_SECRET) {
            console.error(
                'Bridge misconfigured: MAIN_WORKER_URL and MAIL_RELAY_SECRET must be set'
            )
            message.setReject('Bridge misconfigured')
            return
        }

        let rawEmail: string
        try {
            rawEmail = await new Response(message.raw).text()
        } catch (error) {
            console.error('Failed to read raw email:', error)
            message.setReject('Failed to read email')
            return
        }

        const body = JSON.stringify({
            from: message.from,
            to: message.to,
            rawEmail,
            messageId: message.headers.get('Message-ID'),
        })

        const headers: Record<string, string> = {
            'Content-Type': 'application/json',
            'X-Relay-Secret': env.MAIL_RELAY_SECRET,
        }

        // Add password auth if main Worker has password protection
        if (env.MAIN_WORKER_PASSWORD) {
            headers['x-custom-auth'] = env.MAIN_WORKER_PASSWORD
        }

        try {
            const res = await fetch(
                `${env.MAIN_WORKER_URL.replace(/\/$/, '')}/external/api/relay_email`,
                {
                    method: 'POST',
                    headers,
                    body,
                }
            )

            if (!res.ok) {
                const respText = await res.text()
                console.error(
                    `Relay rejected: HTTP ${res.status} - ${respText}`,
                    `from=${message.from} to=${message.to}`
                )
            } else {
                console.log(
                    `Relayed successfully: from=${message.from} to=${message.to}`
                )
            }
        } catch (error) {
            console.error('Relay fetch error:', error)
        }
    },
}
