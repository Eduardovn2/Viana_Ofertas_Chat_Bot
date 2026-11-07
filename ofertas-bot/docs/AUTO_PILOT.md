# AUTO_PILOT — desenvolvimento e execução offline

Controlador em `app/services/auto_pilot.py`, usando `ShopeeDiscoveryService`,
`ShopeeConnector.discover_offers`, comparação multiloja, validação de ofertas manuais,
roteamento, renderizador, outbox e publicador Telegram existentes. A fila e o escopo
ficam em registros `AppSetting` próprios (`auto_pilot:{UUID}`), sem migração ou novo
histórico de preço. Não é instalado gerador de links ou scheduler.

## Funcionamento implementado

- Plano `PREPARED` selado por hash, UUID, credenciais por fingerprint, URLs, destino e
  chat, termos, página/limite, janela e orçamentos. UUID não troca de escopo.
- Início explícito `ACTIVE_MOCK`; descoberta por termos distintos, página 1/limite 3,
  sem retry, fallback ou paginação complementar. Reserva é persistida antes da consulta.
- Anúncios sem evidência comercial suficiente permanecem inconclusivos. O controlador
  não preenche `verified`, disponibilidade, SKU, link, cupom ou documentos.
- Reaproveita ofertas atuais e verificadas do fluxo manual. Para automação exige ainda
  identidade documental explícita: fatos estruturados existentes `ProductFacts`,
  variante registrada, referência, ator, data, fingerprint da fonte e confirmação
  explícita de vínculo. Não exige segunda loja para a oferta individual.
- O campo opcional `documented_identity` foi integrado ao cadastro manual existente;
  é validado antes de criar a nova observação e revalidado no leitor de ofertas.
  Nenhum histórico operacional existente foi alterado. Os fatos documentais não são
  inferências de título nem confirmação independente pela plataforma.
- Seleção revalida nome/identidade, preço anunciado sem faixa ambígua, variante,
  evidências documentais, preço público aplicável, disponibilidade, link próprio,
  validade, categoria e destino. Mudança documental na fila invalida a seleção.
- Aprovação automática registrada como `AUTO_PILOT_RULES:{UUID}`, sem se fazer passar
  por aprovação humana. Usa a aprovação e revalidação da outbox existente.
- Até cinco tentativas por janela **deslizante** de 600 segundos; elegíveis podem
  seguir em sequência. Apenas este perfil substitui o intervalo fixo entre mensagens.
  Mantém teto horário do destino, retry-after, cooldown e proteções da outbox.
- Fila evita repetir anúncio/loja/destino; fatos documentados iguais também impedem
  repetir o mesmo produto de outra loja. Incerteza mantém bloqueio sem prazo automático.
- Lease por destino serializa workers; reserva, tentativa e permissão de HTTP são
  persistidas antes do transporte. Orçamento não é devolvido em falha/timeout.
- Prazo e orçamento de mensagens encerram o piloto; esgotar consultas encerra após
  o processamento da rodada corrente. Parada administrativa impede novas reservas.
  Uma requisição já iniciada pode terminar; nunca é repetida automaticamente.
- `tick` processa a fila e busca até duas páginas quando há capacidade. Janela cheia
  não consome novas consultas. Nenhuma thread foi iniciada; o relógio é conferido
  em cada tick, reserva e permissão final de HTTP, inclusive após reiniciar o processo.
- Relatório preserva descobertas, anúncios distintos, fila, aprovadas pelas regras,
  publicadas, rejeições, inconclusivas, reservas e resultados incertos. `CLAIMED` sem
  resultado após interrupção permanece consumido, sem reexecução automática.

## Administração offline

Endpoints autenticados em `/api/v1/auto-pilots`:

- `POST /`: preparar plano; `GET /{uuid}`: relatório.
- `POST /{uuid}/start-offline`: hash exato do escopo.
- `POST /{uuid}/tick-offline`: executar rodada, sem aprovar cada mensagem.
- `POST /{uuid}/stop`: parada persistente.

**Guardas efetivas:** banco SQLite de testes/simulação ou schema UUID isolado de
`ofertas_bot_test`, flags reais desligadas e MockTransport explícito no conector e no
publicador. A verificação considera o transporte selecionado para o host, bloqueando
montagem HTTP real que tente contornar um transporte padrão mockado. Banco operacional
é recusado. O backend assistido em execução não foi reiniciado ou convertido em piloto.

## Proposta para a etapa real

[Configuração proposta](AUTO_PILOT_PROPOSTA.json): duas horas, até 20 tentativas de
publicação, cinco/600 segundos, 12 consultas, máximo 32 tentativas HTTP no total,
cooldown mínimo de três horas, consulta `productOfferV2` página 1/limite 3, zero links
novos e zero retry/fallback. Termos propostos são parâmetros de busca, não provas de
ofertas. Um worker próprio poderia chamar ticks a cada 30 segundos; não foi iniciado.

O conector de ofertas atual não comprova sozinho todas as variantes, identidade,
estoque/disponibilidade e condições públicas. Sem fonte documental/comercial vigente,
o resultado correto é **zero publicações**, mesmo com alta pontuação textual.
Cupom, queda histórica e equivalência multiloja não são requisitos universais.
Diferença entre lojas, comissão, frete e reputação nunca são inventados.

O laboratório descrito acima continua exclusivo de MockTransport/banco isolado.
O adaptador real separado foi implementado posteriormente sem remover essas guardas:
ver [AUTO_PILOT_REAL.md](AUTO_PILOT_REAL.md). A ativação exige plano real específico,
assinatura válida e aprovação expressa; trocar uma flag não concede autorização.

Para uma autorização única futura: definir termos definitivos, início/fim UTC,
destino/chat, dados e regras elegíveis, limites acima e fingerprints de credenciais;
gerar novo UUID/hash específico e obter confirmação explícita do responsável para
aquele piloto, incluindo ativação controlada e no máximo 20 envios. O despacho deve
conferir esse escopo em cada reserva/HTTP e encerrar por prazo, orçamento ou parada.
A autorização do piloto concede a aplicação automática dessas regras, não aprovação
genérica de produtos ou de publicações fora do escopo. Não reutilizar UUIDs consumidos.

## Validação

Testes em `tests/test_auto_pilot.py` e `tests/test_postgres_auto_pilot.py`, com integrações
anteriores de ofertas manuais e modo assistido. Cobrem lote, janela deslizante, orçamento
de 20, tempo, parada, concorrência, cooldown entre pilotos, zero elegíveis, alterações
de evidência, credencial/destino/escopo e timeout sem retry.

Resultado: **23 testes AUTO_PILOT aprovados** na validação final e **15 PostgreSQL
isolados aprovados**. A regressão com ofertas manuais e inicialização assistida passou
com **80 testes**; contagens se sobrepõem e não devem ser somadas. Ruff e formatação
aprovados. Guardas de HTTP real registraram zero tentativas externas.

Demonstração em `.runtime/demo_auto_pilot.py`: seis candidatos fictícios verificados,
duas consultas simuladas, cinco publicações em sequência no MockTransport, nenhuma
publicação adicional na mesma janela e parada persistida. Resultado estruturado em
`.runtime/auto-pilot-synthetic-result.json`. Zero HTTP externo ou alterações operacionais.
