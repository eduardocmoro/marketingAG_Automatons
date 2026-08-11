# Skill: Contato Multi-Canal com Agentes

## Quando usar
Quando precisar enviar uma proposta de marketing, mensagem ou follow-up para um agente descoberto via ERC-8004 ou para um humano/empresa.

## Sequência de tentativa (ordem de prioridade)

### Método 1 — HTTP Webhook (melhor para agentes)
1. Verificar se o agent card tem campo `services` com tipo `webhook`, `http` ou `messaging`
2. Fazer POST para o `endpoint` encontrado com:
   ```json
   {
     "from": "<meu endereço wallet>",
     "fromName": "AgentLaunch HQ",
     "subject": "Proposta de Marketing B2A",
     "message": "<proposta>",
     "timestamp": "<ISO 8601>",
     "protocol": "erc8004/v1"
   }
   ```
3. Timeout: 10 segundos
4. Sucesso: qualquer status 2xx

### Método 2 — Email
1. Verificar se o agent card tem `contact.email` ou `services[].type === "email"`
2. Se SENDGRID_API_KEY disponível: usar SendGrid REST API
3. Endpoint: `POST https://api.sendgrid.com/v3/mail/send`
4. Se não disponível: registrar o email encontrado para follow-up manual

### Método 3 — Conway Relay (fallback, esperado falhar)
1. Usar endpoint `POST https://api.conway.tech/v1/relay/send`
2. Conway não aceita novos registros — registrar o erro mas não tratar como bloqueador

## Template de proposta de marketing

```
Olá, [Nome do Agente]!

Sou o AgentLaunch HQ, uma agência de marketing autônoma B2A registrada no ERC-8004 na rede Base.

Identificamos que você é [cargo/tipo do agente] e gostaríamos de apresentar nossos serviços:

• Estratégia de conteúdo para audiências de agentes autônomos
• Otimização de agent card para maior visibilidade no ERC-8004
• Campanhas de outreach B2A com métricas de engajamento
• Consultoria de posicionamento no ecossistema Base/Conway

Oferecemos análise gratuita do seu agent card como ponto de partida.

Endereço: [meu wallet]
Protocolo: ERC-8004 / Base mainnet
```

## Logging obrigatório
Após cada tentativa, registrar:
- Agente alvo (ID + endereço)
- Método tentado
- Resultado (sucesso/falha + código de erro)
- Próxima ação planejada

## Critério de sucesso
Qualquer um dos 3 métodos retornar resposta 2xx OU o agente registrar confirmação de recebimento.
