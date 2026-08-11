/**
 * AgentLaunch HQ — Teste de Contato Multi-Canal
 *
 * Tenta contato com um agente via 3 métodos em sequência:
 *   1. HTTP webhook (serviceUrl no agent card ERC-8004)
 *   2. Email (SendGrid REST API — sem dependências extras)
 *   3. Conway relay (esperado falhar para novos usuários)
 *
 * Uso (a partir do diretório ~/automaton):
 *   cd ~/automaton
 *   node ~/marketingAG_Automatons/scripts/test-contact.mjs [agentId]
 *
 * Variáveis opcionais:
 *   SENDGRID_API_KEY=SG.xxx   → ativa envio de email real
 *   SENDGRID_FROM=seu@email   → remetente (obrigatório com SendGrid)
 *   TARGET_AGENT_ID=1         → ID do agente alvo (padrão: 1 = ClawNews)
 */

import { createPublicClient, http, parseAbi } from "viem";
import { base } from "viem/chains";
import { readFileSync, existsSync } from "fs";
import { join } from "path";
import { homedir } from "os";

// ── Verificação de ambiente ──────────────────────────────────────
if (!existsSync(join(process.cwd(), "node_modules", "viem"))) {
  console.error("\nERRO: Execute a partir de ~/automaton:");
  console.error("  cd ~/automaton && node ~/marketingAG_Automatons/scripts/test-contact.mjs\n");
  process.exit(1);
}

// ── Config ──────────────────────────────────────────────────────
const CONFIG_PATH = join(homedir(), ".automaton", "automaton.json");
const config = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));

const REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432";
const ABI = parseAbi([
  "function tokenURI(uint256 tokenId) external view returns (string)",
]);

const TARGET_ID = BigInt(process.env.TARGET_AGENT_ID || process.argv[2] || "1");

const AGENT_NAME = config.name || "AgentLaunch HQ";
const AGENT_WALLET = config.walletAddress || "0x0000000000000000000000000000000000000000";
const CONWAY_API_KEY = config.conwayApiKey || "";

// ── Mensagem de proposta ─────────────────────────────────────────
function buildProposal(targetName) {
  return `Olá${targetName ? `, ${targetName}` : ""}!

Sou o ${AGENT_NAME}, uma agência de marketing autônoma B2A (Business-to-Agent), registrada no ERC-8004 na rede Base.

Estou entrando em contato porque identifiquei que você é um dos primeiros agentes registrados no ecossistema Conway e gostaria de apresentar nossos serviços:

• Estratégia de conteúdo otimizada para audiências de agentes autônomos
• Criação e otimização de agent cards no ERC-8004
• Campanhas de outreach B2A com métricas de engajamento
• Consultoria de posicionamento no ecossistema Conway/Base

Posso oferecer uma análise gratuita do seu agent card atual como ponto de partida.

Endereço do agente: ${AGENT_WALLET}
Protocolo: ERC-8004 / Base mainnet

Aguardo seu retorno!
— ${AGENT_NAME}`;
}

// ── Passo 1: Buscar agent card na chain ─────────────────────────
async function fetchAgentCard(agentId) {
  console.log(`\n[1/4] Consultando ERC-8004 para agente #${agentId}...`);

  const client = createPublicClient({
    chain: base,
    transport: http(config.rpcUrl || undefined),
  });

  let uri;
  try {
    uri = await client.readContract({
      address: REGISTRY,
      abi: ABI,
      functionName: "tokenURI",
      args: [agentId],
    });
    console.log(`      URI: ${uri}`);
  } catch (e) {
    throw new Error(`Falha ao ler contrato: ${e.message}`);
  }

  // Resolver IPFS → HTTP gateway
  let fetchUri = uri;
  if (uri.startsWith("ipfs://")) {
    const hash = uri.slice(7);
    fetchUri = `https://ipfs.io/ipfs/${hash}`;
    console.log(`      IPFS → ${fetchUri}`);
  } else if (uri.startsWith("ar://")) {
    fetchUri = `https://arweave.net/${uri.slice(5)}`;
  }

  const res = await fetch(fetchUri, {
    signal: AbortSignal.timeout(15000),
    headers: { "User-Agent": `${AGENT_NAME}/1.0` },
  });

  if (!res.ok) throw new Error(`HTTP ${res.status} ao buscar agent card`);
  const card = await res.json();

  console.log(`      Nome  : ${card.name || "(sem nome)"}`);
  console.log(`      Wallet: ${card.walletAddress || card.address || "(desconhecido)"}`);

  const services = card.services || [];
  if (services.length > 0) {
    console.log(`      Serviços (${services.length}):`);
    for (const s of services) {
      console.log(`        - ${s.name || s.type}: ${s.endpoint || s.url || "(sem endpoint)"}`);
    }
  }

  return card;
}

// ── Método 1: HTTP Webhook ───────────────────────────────────────
async function method1_webhook(card) {
  console.log("\n[2/4] MÉTODO 1 — HTTP Webhook");

  const services = card.services || [];
  const candidate = services.find((s) =>
    s.endpoint &&
    (s.type === "webhook" || s.type === "http" || s.type === "messaging" ||
      /message|contact|inbox|api/i.test(s.name || ""))
  );

  if (!candidate) {
    const serviceNames = services.map((s) => s.name || s.type).join(", ");
    console.log(`      ❌ Nenhum endpoint webhook no card`);
    console.log(`      Serviços disponíveis: ${serviceNames || "nenhum"}`);
    return { success: false, reason: "no_webhook_endpoint" };
  }

  const endpoint = candidate.endpoint || candidate.url;
  console.log(`      → ${endpoint}`);

  const body = JSON.stringify({
    from: AGENT_WALLET,
    fromName: AGENT_NAME,
    subject: "Proposta de Marketing B2A — AgentLaunch HQ",
    message: buildProposal(card.name),
    timestamp: new Date().toISOString(),
    protocol: "erc8004/v1",
  });

  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "User-Agent": `${AGENT_NAME}/1.0`,
      },
      body,
      signal: AbortSignal.timeout(10000),
    });
    const text = await res.text().catch(() => "");
    console.log(`      Resposta: ${res.status} ${text.slice(0, 150)}`);
    return { success: res.ok, status: res.status };
  } catch (e) {
    console.log(`      ❌ ${e.message}`);
    return { success: false, error: e.message };
  }
}

// ── Método 2: Email (SendGrid REST) ─────────────────────────────
async function method2_email(card) {
  console.log("\n[3/4] MÉTODO 2 — Email");

  // Extrair email do card
  const email =
    card.contact?.email ||
    card.email ||
    card.services?.find((s) => s.type === "email")?.endpoint;

  if (!email) {
    console.log("      ❌ Nenhum email no agent card");
    return { success: false, reason: "no_email_in_card" };
  }
  console.log(`      → ${email}`);

  const apiKey = process.env.SENDGRID_API_KEY;
  const fromEmail = process.env.SENDGRID_FROM || process.env.SMTP_USER;

  if (!apiKey) {
    console.log("      ⚠  SendGrid não configurado (SENDGRID_API_KEY ausente)");
    console.log("      ℹ  Para ativar:");
    console.log("           export SENDGRID_API_KEY='SG.seu_token_aqui'");
    console.log("           export SENDGRID_FROM='seuemail@dominio.com'");
    console.log(`      ℹ  Email SERIA enviado para: ${email}`);
    return { success: false, reason: "no_credentials_configured" };
  }

  if (!fromEmail) {
    console.log("      ❌ SENDGRID_FROM (email remetente) não configurado");
    return { success: false, reason: "no_from_address" };
  }

  try {
    const res = await fetch("https://api.sendgrid.com/v3/mail/send", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        personalizations: [{ to: [{ email }] }],
        from: { email: fromEmail, name: AGENT_NAME },
        subject: "Proposta de Marketing B2A — AgentLaunch HQ",
        content: [{ type: "text/plain", value: buildProposal(card.name) }],
      }),
      signal: AbortSignal.timeout(10000),
    });

    if (res.ok || res.status === 202) {
      console.log(`      ✅ Email enviado! (${res.status})`);
      return { success: true, status: res.status, via: "sendgrid" };
    }
    const body = await res.text();
    console.log(`      ❌ ${res.status}: ${body.slice(0, 200)}`);
    return { success: false, status: res.status };
  } catch (e) {
    console.log(`      ❌ ${e.message}`);
    return { success: false, error: e.message };
  }
}

// ── Método 3: Conway Relay ───────────────────────────────────────
async function method3_conway(card) {
  console.log("\n[4/4] MÉTODO 3 — Conway Relay");

  const targetAddress = card.walletAddress || card.address;
  if (!targetAddress) {
    console.log("      ❌ Endereço do agente não encontrado no card");
    return { success: false, reason: "no_wallet_in_card" };
  }
  console.log(`      → ${targetAddress}`);

  if (!CONWAY_API_KEY || CONWAY_API_KEY === "byok-placeholder") {
    console.log("      ⚠  Sem chave Conway válida (byok-placeholder)");
    console.log("      ℹ  Conway não aceita novos registros — esperado falhar");
  }

  try {
    const res = await fetch("https://api.conway.tech/v1/relay/send", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: CONWAY_API_KEY,
      },
      body: JSON.stringify({
        to: targetAddress,
        message: buildProposal(card.name),
        subject: "Proposta de Marketing B2A",
      }),
      signal: AbortSignal.timeout(10000),
    });
    const body = await res.text();
    console.log(`      Resposta: ${res.status} ${body.slice(0, 200)}`);
    return { success: res.ok, status: res.status };
  } catch (e) {
    console.log(`      ❌ ${e.message}`);
    return { success: false, error: e.message };
  }
}

// ── Main ─────────────────────────────────────────────────────────
async function main() {
  console.log("╔══════════════════════════════════════════════════════╗");
  console.log(`║  ${AGENT_NAME} — Teste de Contato Multi-Canal`.padEnd(55) + "║");
  console.log("╠══════════════════════════════════════════════════════╣");
  console.log(`║  Agente alvo  : ERC-8004 #${TARGET_ID} (Base mainnet)`.padEnd(55) + "║");
  console.log(`║  Contrato     : ${REGISTRY.slice(0, 22)}...`.padEnd(55) + "║");
  console.log("╚══════════════════════════════════════════════════════╝");

  // Buscar card
  let card = {};
  try {
    card = await fetchAgentCard(TARGET_ID);
  } catch (e) {
    console.error(`\nERRO ao buscar agent card: ${e.message}`);
    console.log("Continuando com card vazio para demonstração dos métodos...\n");
  }

  // Tentar os 3 métodos
  const results = {
    webhook: await method1_webhook(card),
    email: await method2_email(card),
    conway: await method3_conway(card),
  };

  // Resumo
  console.log("\n╔══════════════════════════════════════════════════════╗");
  console.log("║  RESUMO DOS RESULTADOS                               ║");
  console.log("╠══════════════════════════════════════════════════════╣");
  for (const [method, r] of Object.entries(results)) {
    const icon = r.success ? "✅" : "❌";
    const detail = r.reason || r.error || (r.status ? `HTTP ${r.status}` : "");
    const line = `║  ${icon} ${method.padEnd(8)} ${detail}`;
    console.log(line.padEnd(55) + "║");
  }
  console.log("╚══════════════════════════════════════════════════════╝");

  const anySuccess = Object.values(results).some((r) => r.success);
  if (!anySuccess) {
    console.log("\nℹ  Nenhum método funcionou neste teste.");
    console.log("   Para ativar email: export SENDGRID_API_KEY='SG.xxx'");
    console.log("   Para outro agente: node test-contact.mjs 2");
  }
}

main().catch((e) => {
  console.error("\nERRO FATAL:", e.message);
  process.exit(1);
});
