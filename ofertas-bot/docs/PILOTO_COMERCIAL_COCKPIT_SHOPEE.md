# Piloto comercial controlado — Cockpit Brave Bull — sem envio

**Ambiente validado; avaliação comercial BLOCKED.** Preparação documental offline, sem criação de produto, oferta, atestado manual, aprovação, prévia ou outbox. [Evidências e rascunhos estruturados](PILOTO_COMERCIAL_COCKPIT_SHOPEE.json).

## 1. Ambiente e testes PostgreSQL

- Windows local / PowerShell; Docker Desktop acessível, Engine **29.8.2 Linux**.
- Container Compose `postgres` em execução e saudável; PostgreSQL **17.11**.
- Conexões reais ao banco operacional em transações **READ ONLY** e ao banco exclusivo **ofertas_bot_test** confirmadas.
- O banco de testes já existia. Não foi necessário recriar banco, iniciar/reinstalar serviços, alterar senhas, `.env`, configurações globais ou volumes. A indisponibilidade anterior não se reproduziu com o Engine ativo.
- **80 testes PostgreSQL aprovados, zero falhas e zero ignorados**; 1.422 testes offline excluídos dessa seleção. Todos em schemas UUID exclusivos, com respostas HTTP mockadas.
- Alembic PostgreSQL `upgrade head`, `current` e `check` executados nos schemas de testes; revisão **0009_shopee_discovery (head)**, **No new upgrade operations detected**. O caso `test_postgres_migration_current_check` foi repetido com captura explícita da saída: um aprovado, já incluído nos 80 casos, sem somar como teste novo.
- Um aviso de depreciação de dependência. Suíte offline anterior não repetida: não houve alteração no código da aplicação/testes nesta execução.
- Transporte HTTP real bloqueado durante os testes; nenhuma tentativa alcançou esse transporte. Não foi iniciado backend ou scheduler operacional.

As fixtures criaram e removeram somente seus próprios schemas temporários de testes. As tabelas e os schemas anteriormente existentes no banco de testes permaneceram idênticos. Nada foi removido de bancos/volumes existentes.

## 2. Estado operacional conferido somente por leitura

- Viana Ofertas, Chat ID **-5309308689**: **enabled=false**, aprovação manual obrigatória, categorias `all`.
- **Nove categorias ativas** preservadas; demais destinos/configurações sem alteração.
- Monitoramento persistido **scheduler_enabled=false**; intervalos existentes 60/300 segundos preservados. O valor local de configuração padrão do scheduler não foi alterado nem usado para iniciar execução.
- **SHOPEE_ENABLED=false**, **MERCADO_LIVRE_ENABLED=false**, **TELEGRAM_PUBLISHER_ENABLED=false**, **TELEGRAM_REAL_SENDS_AUTHORIZED=false**, **SIMULATION_AUTOMATION_ENABLED=false** e **SHOPEE_SHORT_LINKS_ENABLED=false**.
- Hashes de todas as linhas das **32 tabelas operacionais**, das tabelas/linhas públicas e da lista de schemas anteriores do banco de testes, do `.env` e dos artefatos históricos conferidos antes/depois, sem alterações.

## 3. Evidências existentes do link real

- Produto de referência fornecido pelo responsável: **Cockpit simulador de corrida Brave Bull**, shopId **1904573791**, itemId **23199869203**.
- Reserva histórica `shopee_shortlink_once:50f837480acf995acb3a4eabd3901a113407042b16177d9fdf1a80442e95d9f9`: **SUCCEEDED**, autorização consumida, **uma tentativa histórica**, HTTP **200**, um POST histórico.
- Hash do corpo reconstruído localmente a partir do payload reservado coincide com os hashes dos registros. A reconstrução foi somente de bytes para conferência de hash, sem execução da mutation.
- Fluxo histórico usa o conector e assinatura com configuração local; HTTP 200 e resultado oficial são evidência histórica da geração autenticada. Isso não constitui nova validação da credencial ou da conta perante a Shopee.
- Parser comum atual confirma shopId/itemId tanto na origem quanto no caminho `/universal-link/` armazenado, sem resolver URL ou consultar a loja. Não comprova variante, disponibilidade, comissão ou titularidade.
- shortLink oficial conservado e consistente entre registros. Não foi gerado, convertido, resolvido ou associado a produto nesta execução.
- **Limitação de rastreamento identificada:** o longLink persistido tem cinco parâmetros; **um valor contém marcador de redação**. O script histórico aplicava sanitização antes de persistir o resultado. Portanto, não é possível atestar a preservação integral do longLink original. Os registros atuais estão íntegros e inalterados; não tentamos substituir o marcador, inferir o valor original ou repetir a mutation. O longLink sanitizado não deve ser usado como URL de compra. A limitação não invalida automaticamente o shortLink oficial, mas exige decisão administrativa documentada sobre sua utilização.
- IntegrationEvent **6 RESERVED**, **7 DISPATCH_CLAIMED**, **8 SUCCEEDED**, **9 IDENTITY_PATH_MATCH** conferidos. Reserva e esses quatro registros mantêm os hashes da baseline histórica anterior. O log HTTP e o resultado do banco diferem apenas na lista de eventos que foi completada depois; o registro FINAL acrescenta metadados de encerramento. Os links armazenados coincidem.
- Credenciais presentes na configuração local, sem exposição. Não encontramos documento que vincule essas credenciais/conta ao beneficiário do link, atestado de titularidade, conversão ou comissão validada. **account_verified permanece false.** Nenhuma venda anterior é exigida automaticamente como prova universal.

A constatação sobre rastreamento qualifica relatórios anteriores de preservação: foi comprovada a integridade dos dados já sanitizados, não a existência da resposta bruta integral. Nenhum registro anterior foi reescrito.

## 4. Dados comerciais específicos do Cockpit

**Não existe cadastro comercial desse produto no PostgreSQL operacional, nem observação de preço vinculada a ele.** A mutation de links retornou links, não uma cotação comercial. Nenhuma informação de outro produto foi transferida para o Cockpit.

| Campo | Evidência atual | Consequência |
|---|---|---|
| Nome e shopId/itemId | Referência do responsável e auditoria da mutation | Identidade do anúncio confirmada no escopo registrado |
| Preço anunciado | Não encontrado | Falta informação real documentada |
| Preço público aplicável | Não comprovado | Bloqueia a prévia comercial |
| Variante/modelo da oferta | Não comprovado | Falta identificar a configuração exata abrangida pelo preço |
| Estoque/disponibilidade | Desconhecido | Bloqueia a prévia comercial |
| Condições de pagamento do preço | Desconhecidas | Confirmar preço à vista/cartão/PIX e restrições; parcelamento é opcional e deve ser omitido se não comprovado |
| Cupom | Nenhum comprovado | Opcional; não inventar cupom ou desconto |
| Frete/CEP aplicável | Desconhecidos | Não afirmar frete grátis, prazo ou custo |
| Imagem | Não documentada neste escopo | Opcional; não acrescentada |
| Histórico comparável | Zero observações do Cockpit | Sem evidência de queda, preço anterior ou percentual de desconto |
| Comissão | Não validada | Nunca usada como desconto ao consumidor |

Uma primeira observação futura válida deverá ser INITIAL, sem anunciar queda histórica. Não foi criada observação fictícia para produzir esse estado. Cupons, imagem, preço anterior e parcelamento não são requisitos universais quando omitidos da mensagem; preço aplicável, identidade/variante, disponibilidade e condições relevantes precisam estar comprovados.

## 5. Proposta administrativa de confiança — sem ativação

Rascunho documental limitado ao anúncio acima e ao shortLink oficial já gerado:

1. Exigir geração autenticada historicamente auditada e identidade compatível.
2. Exigir documentação administrativa não secreta de proveniência da credencial/conta e vínculo/beneficiário do link.
3. Exigir decisão explícita sobre o uso do shortLink oficial diante da sanitização parcial histórica do longLink. Não reconstruir nem cadastrar o longLink sanitizado como URL utilizável.
4. Manter conversão e comissão como evidências independentes opcionais; não exigir uma venda anterior universalmente.
5. Exigir evidência comercial recente, independente e rastreável para o preço e a variante/disponibilidade aplicáveis.
6. Registrar responsável, referência documental, horário e decisão quando forem efetivamente fornecidos. Nenhum foi preenchido ou inferido nesta execução.

Estado **PROPOSED_DOCUMENT_ONLY**, sem decisão humana registrada, sem aprovação, sem persistência de nova política em AppSetting e sem promoção de account_verified. O JSON contém também um rascunho de evidências comerciais **UNCONFIRMED_DOCUMENT_ONLY**, com campos desconhecidos nulos, sem representar um atestado humano. Alterar preferências ou aprovar confiança futuramente não concede autorização de rede/publicação.

## 6. Resultado da prévia

**BLOCKED — nenhuma prévia/oferta/outbox criada.** Faltam cadastro real, preço aplicável, variante, disponibilidade, condições do preço, proveniência/vínculo da conta e política efetivamente aprovada.

Não há produto/observação persistidos para passar a PriceComparisonService ou OfferEngine. Esses serviços não foram invocados com dados fabricados. A ausência de uma política aprovada também impede passar ao serviço de prévia comercial. O fluxo não foi forçado para gerar sucesso.

## 7. Próximo passo mínimo

O responsável deve reunir localmente evidência datada e referenciada do **preço público aplicável ao Cockpit, variante exata, disponibilidade e condições desse preço**, além da documentação da conta/beneficiário e da decisão sobre o shortLink. Usar os serviços administrativos existentes para cadastrar os dados reais e registrar atestados explícitos somente após essa confirmação. Aprovar separadamente a política proposta e então reavaliar apenas a prévia. Não é necessário gerar outro link ou comprovar uma venda apenas para começar essa avaliação. Não associar o link ao cabo HDMI de outra consulta.

Nenhuma consulta autenticada, generateShortLink, scraping ou publicação faz parte desse próximo passo documental. Uma eventual consulta ou publicação real precisará de autorização específica posterior.

## 8. Segurança e arquivos

**Zero novas chamadas Shopee; zero mutations; zero publicações Telegram.** Autorização antiga não reutilizada; `.env`, credenciais, histórico, auditoria e demais destinos preservados. Nenhum commit/push ou avanço de fase.

Somente documentação versionável foi alterada: este relatório, o JSON do piloto, README e ROADMAP. Scripts/logs locais de validação ficam ignorados em `.runtime`; não há alteração na aplicação, migrations ou testes. Evidências locais: `pilot-postgres.log`, `pilot-alembic.log`, `pilot-preservation.json` e `pilot-commercial-before.json`.
