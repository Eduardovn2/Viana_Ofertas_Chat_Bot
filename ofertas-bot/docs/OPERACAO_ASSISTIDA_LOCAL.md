# Operação local assistida — pronta para completar a primeira oferta

**Backend iniciado e conferido:** `http://127.0.0.1:8000`, `/health` HTTP 200,
PostgreSQL `ofertas_bot` conectado. Autenticação administrativa conferida. Não há
exposição em interfaces externas, reload, scheduler ou envios ativos.

Início repetível: `scripts/start_assisted_backend.ps1`. Usa a aplicação existente por
`app.assisted:create_assisted_app`. A configuração segura é somente deste processo;
não altera `.env`, credenciais ou flags persistidas. Não substitui processo se a porta
estiver ocupada. PID/logs em `.runtime/assisted-backend.*`.

O modo assistido impede a criação do scheduler no startup e rejeita tentativa de
retomada por API com HTTP 409. Integrações externas, geração de links, OAuth, simulação
automática e envios ficam desativados no processo. O destino não foi habilitado.

## Correção prática no fluxo existente

`POST /api/v1/manual-offers/{offer_id}/review-message`

Corpo: `{"destination_id": 2, "include_image": false}`. Requer administrador e oferta
manual já verificada, vigente, com link próprio verificado, roteamento e cooldown
válidos. Mostra a mensagem pelo **mesmo renderizador do publicador** mesmo com grupo
pausado. Não grava outbox, não ativa o destino e não aprova nem envia. Retorna conteúdo,
destino, validade e hash de revisão do conteúdo, que **não é o hash de aprovação da
outbox**. A prévia/aprovação/despacho atuais mantêm todos os bloqueios existentes.

Isso permite revisar uma mensagem concreta antes da autorização de ativação/envio.
Após a autorização, a execução deve conferir que mensagem e chat da prévia normal
são exatamente os revisados, revalidar a validade e registrar a aprovação vinculada
ao hash atual da outbox. Mudança exige nova revisão; não há aprovação por equivalência
de texto, título, preço ou hashes de formulários.

## Produto escolhido e impedimentos reais

Produto **2**, Cockpit simulador de corrida Brave Bull,
**1904573791:23199869203**, opção **AZUL declarada**. Cadastro real preservado; nenhum
SKU inferido, link associado, oferta ou publicação comercial criado.

O link preservado corresponde ao registro auditado; não é necessário gerar outro.
Restam somente os dados não comprovados:

- Referência documental que vincule o link existente à conta/beneficiário. A declaração
  de titularidade foi preservada, mas não equivale a verificação operacional.
- Página/descrição da opção AZUL: modelo, quantidade, conteúdo do kit e itens excluídos;
  seleção documentada e vínculo ao anúncio. SKU somente se existir em fonte oficial.
- No momento do cadastro: preço público aplicável, pagamento/elegibilidade,
  disponibilidade, responsável, horário com fuso e validade máxima de 600 segundos.
  Preços antigos não foram usados como atuais.

O rascunho comercial está em `FORMULARIO_OFERTA_MANUAL_PRODUTO_2_PENDENTE.json`.
Mantém preço ausente, `verified=false`, disponibilidade/acesso desconhecidos e a
seleção/titularidade declaradas separadas das provas. Não foi submetido.

## Caminho para um envio, sem nova arquitetura

1. Receber as evidências estáveis e confirmar o escopo da opção; associar o **mesmo**
   link pelo endpoint existente somente depois da comprovação explícita.
2. Confirmar as condições comerciais atuais, completar o rascunho e cadastrar a
   oferta individual pelo fluxo manual existente. Não exige comparação multiloja.
3. Usar `review-message` com o grupo pausado e apresentar a mensagem exata, destino e
   validade. Pedir **uma autorização de publicação** que abranja esse conteúdo,
   grupo **-5309308689**, ativação controlada necessária e **um único envio**.
4. Somente após essa autorização: abrir os bloqueios necessários em execução
   controlada, gerar a prévia normal, conferir igualdade exata de mensagem/chat,
   registrar a aprovação no hash atual e despachar uma vez pelos serviços existentes.
   Preservar scheduler desligado. Resultado incerto não permite retry automático;
   encerrar as permissões temporárias conforme o escopo autorizado.

O modo atual é deliberadamente incapaz de despachar: autorização de hash não habilita
flags. A execução futura deve usar configuração de envio explicitamente autorizada;
não houve ativação nesta sessão. Se os dados mudarem ou expirarem, revalidar e revisar.

Validação específica desta entrega: **67 testes aprovados** de startup/API/ofertas
manuais, além da repetição dos dois testes da revisão após ajuste de erros. Ruff
aprovado. Smoke real somente em loopback e leituras operacionais; backend e banco
saudáveis. Zero HTTP externo, links gerados, gravações operacionais, ofertas ou envios.
Credenciais, históricos, autorizações e declarações anteriores preservados. Sem commit/push.
