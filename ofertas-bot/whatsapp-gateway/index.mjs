// Gateway WhatsApp do Viana Ofertas.
//
// Mantém um número de WhatsApp conectado (como "aparelho conectado", via Baileys) e
// expõe uma API HTTP só em 127.0.0.1 para o beta_worker publicar as ofertas:
//   GET  /status  -> { connected, me, pairingCode, qr }
//   GET  /qr      -> página com o QR code para conectar
//   GET  /groups  -> grupos em que o número participa (para achar o WHATSAPP_CHAT_ID)
//   POST /send    -> { jid, text, imageUrl? }  =>  { id }
//
// Configuração lida do ../.env (pasta ofertas-bot) ou de variáveis de ambiente:
//   WHATSAPP_GATEWAY_PORT (3100), WHATSAPP_GATEWAY_TOKEN (opcional),
//   WHATSAPP_PAIRING_NUMBER (opcional, ex.: 5511999998888: conecta por código em vez de QR).

import { existsSync, mkdirSync, readFileSync } from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import makeWASocket, {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys'
import pino from 'pino'
import QRCode from 'qrcode'
import qrcodeTerminal from 'qrcode-terminal'

const here = path.dirname(fileURLToPath(import.meta.url))

function loadDotEnv(file) {
  if (!existsSync(file)) return
  for (const raw of readFileSync(file, 'utf-8').split(/\r?\n/)) {
    const match = raw.match(/^\s*(WHATSAPP_[A-Z_]+)\s*=\s*(.*?)\s*$/)
    if (!match || process.env[match[1]] !== undefined) continue
    process.env[match[1]] = match[2].replace(/^(['"])(.*)\1$/, '$2')
  }
}
loadDotEnv(path.join(here, '..', '.env'))

const PORT = Number(process.env.WHATSAPP_GATEWAY_PORT || 3100)
const TOKEN = process.env.WHATSAPP_GATEWAY_TOKEN || ''
const PAIRING_NUMBER = (process.env.WHATSAPP_PAIRING_NUMBER || '').replace(/\D/g, '')
const AUTH_DIR = path.join(here, '..', '.runtime', 'whatsapp-auth')
const JID = /^([0-9]{5,20}(-[0-9]{5,20})?@g\.us|[0-9]{5,30}@newsletter)$/

mkdirSync(AUTH_DIR, { recursive: true })
const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL || 'warn' })

const state = { sock: null, connected: false, me: null, qr: null, pairingCode: null }

async function connect() {
  const { state: auth, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: undefined }))
  const sock = makeWASocket({
    auth,
    version,
    logger,
    browser: Browsers.windows('Viana Ofertas'),
    markOnlineOnConnect: false,
    syncFullHistory: false,
  })
  state.sock = sock
  sock.ev.on('creds.update', saveCreds)

  if (PAIRING_NUMBER && !auth.creds.registered) {
    setTimeout(async () => {
      try {
        state.pairingCode = await sock.requestPairingCode(PAIRING_NUMBER)
        console.log(`\nCódigo de pareamento: ${state.pairingCode}`)
        console.log('No celular: WhatsApp > Aparelhos conectados > Conectar com número de telefone.\n')
      } catch (error) {
        console.error('Não foi possível gerar o código de pareamento:', error?.message)
      }
    }, 3000)
  }

  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr && !PAIRING_NUMBER) {
      state.qr = qr
      console.log(`\nLeia o QR code (ou abra http://127.0.0.1:${PORT}/qr):`)
      qrcodeTerminal.generate(qr, { small: true })
    }
    if (connection === 'open') {
      state.connected = true
      state.qr = null
      state.pairingCode = null
      state.me = sock.user?.id || null
      console.log(`WhatsApp conectado como ${state.me}`)
    }
    if (connection === 'close') {
      state.connected = false
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) {
        console.error(
          'Sessão encerrada no celular. Apague .runtime/whatsapp-auth e inicie de novo para reconectar.',
        )
        process.exit(1)
      }
      console.warn(`Conexão caiu (código ${code ?? '?'}); reconectando em 5s...`)
      setTimeout(() => connect().catch((e) => console.error(e)), 5000)
    }
  })
}

function reply(res, status, body, type = 'application/json') {
  res.writeHead(status, { 'Content-Type': type })
  res.end(type === 'application/json' ? JSON.stringify(body) : body)
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > 64 * 1024) reject(new Error('too large'))
      else chunks.push(chunk)
    })
    req.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf-8')))
      } catch (error) {
        reject(error)
      }
    })
    req.on('error', reject)
  })
}

async function send({ jid, text, imageUrl }) {
  const sock = state.sock
  if (imageUrl) {
    try {
      return await sock.sendMessage(jid, { image: { url: imageUrl }, caption: text })
    } catch (error) {
      // Falha ao baixar a imagem acontece antes do envio: manda só o texto.
      logger.warn({ err: error?.message }, 'imagem falhou; enviando só texto')
    }
  }
  return sock.sendMessage(jid, { text })
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1')
    if (url.pathname === '/qr' && req.method === 'GET') {
      if (state.connected) return reply(res, 200, '<h2>WhatsApp já conectado.</h2>', 'text/html')
      if (!state.qr) {
        const extra = state.pairingCode ? `Código de pareamento: <b>${state.pairingCode}</b>` : ''
        return reply(res, 200, `<meta http-equiv="refresh" content="3"><p>Aguardando QR... ${extra}</p>`, 'text/html')
      }
      const image = await QRCode.toDataURL(state.qr, { width: 360 })
      return reply(
        res,
        200,
        `<meta http-equiv="refresh" content="20"><h2>WhatsApp > Aparelhos conectados > Conectar aparelho</h2><img src="${image}">`,
        'text/html',
      )
    }
    if (TOKEN && req.headers.authorization !== `Bearer ${TOKEN}`) {
      return reply(res, 401, { error: 'UNAUTHORIZED' })
    }
    if (url.pathname === '/status' && req.method === 'GET') {
      return reply(res, 200, {
        connected: state.connected,
        me: state.me,
        pairingCode: state.pairingCode,
        waitingQr: Boolean(state.qr),
      })
    }
    if (url.pathname === '/groups' && req.method === 'GET') {
      if (!state.connected) return reply(res, 503, { error: 'NOT_CONNECTED' })
      const groups = await state.sock.groupFetchAllParticipating()
      return reply(
        res,
        200,
        Object.values(groups).map((g) => ({ id: g.id, name: g.subject, members: g.participants?.length })),
      )
    }
    if (url.pathname === '/send' && req.method === 'POST') {
      if (!state.connected) return reply(res, 503, { error: 'NOT_CONNECTED' })
      let body
      try {
        body = await readJson(req)
      } catch {
        return reply(res, 400, { error: 'INVALID_MESSAGE' })
      }
      const { jid, text, imageUrl } = body || {}
      if (typeof jid !== 'string' || !JID.test(jid) || typeof text !== 'string' || !text || text.length > 4000) {
        return reply(res, 400, { error: 'INVALID_MESSAGE' })
      }
      if (imageUrl !== undefined && (typeof imageUrl !== 'string' || !/^https:\/\//.test(imageUrl))) {
        return reply(res, 400, { error: 'INVALID_MESSAGE' })
      }
      let message
      try {
        message = await send({ jid, text, imageUrl })
      } catch (error) {
        console.warn('Envio recusado pelo WhatsApp:', error?.message)
        return reply(res, 502, { error: 'WHATSAPP_REJECTED' })
      }
      return reply(res, 200, { id: message?.key?.id || 'sent' })
    }
    return reply(res, 404, { error: 'NOT_FOUND' })
  } catch (error) {
    logger.error({ err: error?.message }, 'erro no gateway')
    return reply(res, 500, { error: 'INTERNAL' })
  }
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(`Gateway WhatsApp ouvindo em http://127.0.0.1:${PORT}`)
})
connect().catch((error) => {
  console.error('Falha ao iniciar o WhatsApp:', error)
  process.exit(1)
})
