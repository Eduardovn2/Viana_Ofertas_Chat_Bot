# VIANA_OFERTAS_CHAT_BOT — FASE 3
## Integrações reais de ofertas: Mercado Livre e Shopee

**Status:** especificação pronta para implementação; não executada.  
**Pré-requisitos conhecidos:** Fases 1, 2 e 2.1 implementadas; migrações até `0003_category_routing` aplicadas ao PostgreSQL local; API `/health` retornou `database: connected`; seed de 9 categorias executado; 241 testes isolados e 2 testes PostgreSQL aprovados em suas execuções respectivas.  
**Meta da fase:** consumir dados comerciais reais por meios permitidos, normalizar e validar ofertas, alimentar histórico/classificação/pontuação/fila **sem enviar mensagens reais**.

> **Instrução ao Codex:** leia primeiro `README.md`, `ROADMAP.md`, `AGENTS.md` (se existir), os modelos, serviços e testes presentes no repositório. A estrutura abaixo é um contrato funcional; adapte nomes e caminhos à arquitetura REAL, em vez de criar implementações paralelas. Preserve os recursos existentes e trabalhe incrementalmente. Documente limitações comprovadas, não invente acessos ou resultados.

## 1. Escopo e limites

**Implementar**
1. Conectores independentes de Mercado Livre e Shopee, desacoplados da camada de domínio.
2. Consultas reais **somente** para recursos autorizados pelas credenciais/permissões disponíveis.
3. Normalização consistente de preço, preço antigo opcional, disponibilidade, moeda, categoria da loja, imagem e URL canônica.
4. Garimpo e monitoramento automáticos onde a API permita; cadastro/manual como alternativa operacional quando não permitir.
5. Registro de evidência da origem e momento da consulta, sem afirmar descontos/cuponagens não verificadas.
6. Descoberta e avaliação conservadora de cupons com proveniência e restrições.
7. Reaproveitar classificação da Fase 2.1, score, prioridades, fila, deduplicação e janela de republicação (3 horas).
8. API administrativa local para status dos conectores, testes controlados, coleta manual e diagnóstico.
9. Feature flags e testes de contrato com fixtures sanitizadas; documentação para habilitação real.

**Não implementar nesta fase**
- Publicação real em Telegram ou WhatsApp, disparos para grupos ou ativação do destino `Ofertas de Casa` (deve permanecer **desativado**).
- SaaS, pagamentos, multiusuário, painel comercial ou credenciais de clientes.
- Automação de interfaces internas do Link Builder do Mercado Livre, reutilização de cookies de sessão ou engenharia de APIs privadas.
- Scraping que viole termos, contorne bloqueios, CAPTCHA, rate limits ou autenticação.
- Preço VIP/assinatura como preço público.
- Geração fictícia de links de afiliado, cupons, preço anterior, comissões ou simulação tratada como dado real.

## 2. Gate de viabilidade por plataforma (primeira tarefa)

**Antes de programar chamadas reais, produzir `docs/INTEGRACOES_FASE_3.md`** contendo por plataforma:
- Documentação oficial consultada, URL e data da consulta.
- Disponibilidade para este tipo de conta (**afiliado**, e não necessariamente vendedor), necessidade de OAuth/App ID/Secret e permissões.
- Operações acessíveis: consultar item, listar ofertas, obter preço/estoque, descobrir cupons, converter links.
- Limites de uso, termos relevantes, campos retornados e restrições de dados.
- Estado: `SUPPORTED`, `REQUIRES_CREDENTIALS`, `UNAVAILABLE_FOR_ACCOUNT` ou `UNVERIFIED` por capacidade, não apenas por loja.
- Resultado de uma chamada de teste apenas quando credenciais autorizadas estiverem configuradas.

Se não houver autorização ou documentação suficiente, implementar o **adaptador desativado com contrato e testes mockados**, registrar bloqueio e continuar demais recursos. Não travar a entrega por ausência de credenciais.

### 2.1 Mercado Livre

- **Afiliados:** manter geração do link **manual** via Link Builder oficial nesta versão. O administrador fornece URL canônica do produto e URL de afiliado própria; validar formato e registrar origem `MANUAL_LINKBUILDER`. Exemplo conhecido: item `MLB3810190843`, link `https://meli.la/1JQSATy` (exemplo cadastrado, não assumir preço atual nem comissão confirmada).
- **Produtos e preços:** investigar APIs oficiais de itens/busca e **API de preços** vigente, inclusive requisitos de autorização. A documentação indica descontinuação dos campos `price`, `base_price` e `original_price` de `/items` como fonte de preços; **não fundamentar o conector novo nesses campos antigos**. Não supor que a API de preços autenticada de vendedor esteja acessível à conta de afiliado.
- **Descoberta de produtos:** utilizar apenas buscas oficiais permitidas para a conta/permissão. Se busca automática não estiver disponível, monitorar apenas os produtos cadastrados e deixar candidatos descobertos como pendentes até existir fonte permitida.
- **Cupons:** a documentação `seller-promotions` trata de cupons de **vendedor**, não prova que se possam pesquisar todos os cupons públicos de terceiros. Não usar APIs de criação/edição de campanha para garimpar cupons e não criar promoções.
- **URLs:** normalizar IDs como `MLB3810190843` sem eliminar identificadores necessários de variante/contexto. Nunca trocar links de outro afiliado por links supostamente próprios nem publicar URLs não validadas.

### 2.2 Shopee

- **Afiliados:** investigar o explorador oficial da Open API de afiliados e as permissões brasileiras. Operações de referência existentes no Explorer incluem `productOfferV2`, `shopeeOfferV2` e `generateShortLink`; confirmar esquema, campos, autenticação, endpoint regional e autorização efetivos antes do uso.
- **Credenciais:** habilitar chamadas apenas se o proprietário tiver acesso válido `AppId`/`Secret` ou outras credenciais exigidas pela versão autorizada da API. Não pedir que usuário cole segredo no chat, nos commits ou nos logs.
- **Produtos/ofertas:** ingestão de ofertas, preços, comissão, disponibilidade e categoria somente para campos realmente retornados. Não calcular `original_price` invertendo uma taxa arredondada nem apresentar o resultado como preço antigo observado.
- **Links:** usar geração automática **apenas** se `generateShortLink` estiver autorizada para a conta e o link gerado puder ser identificado como associado à conta. Caso contrário, permitir cadastro manual de link Shopee, com a mesma proteção de publicação.
- **Cupons:** não assumir que a Open API permite testar aplicação no checkout ou retornar todos os cupons. Guardar `UNVERIFIED` se não houver evidência de aplicabilidade.

**Fontes oficiais iniciais para verificação:**
- Mercado Livre, Preços de produtos: https://developers.mercadolivre.com.br/pt_br/servicos-gerenciamento-de-contatos/api-de-precos
- Mercado Livre, Busca de itens: https://developers.mercadolivre.com.br/pt_br/itens-e-buscas
- Mercado Livre, Cupons do vendedor: https://developers.mercadolivre.com.br/pt_br/api-docs-pt-br/cupons-do-vendedor
- Mercado Livre, Links de afiliado: https://www.mercadolivre.com.br/l/afiliados-gere-seus-links
- Shopee Affiliate Open API Explorer BR: https://open-api.affiliate.shopee.com.br/explorer
- Shopee, Ajuda para gerar links de afiliado: https://help.shopee.com.br/portal/10/article/128461-Como-gerar-links-de-produtos-ou-lojas

Esses endereços servem para **validação pelo Codex**; não garantem disponibilidade de API para a conta do usuário.

## 3. Contratos de domínio e proveniência

Criar ou adaptar um protocolo `OfferSource` / `MarketplaceConnector` com capacidades explícitas, por exemplo:
- `capabilities(): SourceCapabilities`
- `fetch_product(product_ref) -> ProductSnapshot`
- `discover_offers(query, category, cursor) -> Page[ProductSnapshot]` **somente se suportado**
- `fetch_coupons(product_ref) -> list[CouponEvidence]` **somente se suportado**
- `generate_affiliate_link(url) -> AffiliateLinkResult` **somente se suportado**

Usar `UnsupportedCapability` tipada quando a operação não estiver disponível, sem fabricar resultado vazio de sucesso. Evitar API nova desnecessária quando já houver interface apropriada.

### Snapshot mínimo

- `platform`, `external_item_id`, `variant_id` quando necessário, `canonical_url`, `name`, `category_external_id`, `category_external_name`.
- `current_price: Decimal | None`, `listed_old_price: Decimal | None`, `currency`, `availability` (disponível/indisponível/desconhecido), `image_url` quando permitido.
- `seller_id` quando disponível, `price_access` (`PUBLIC`, `CONDITIONAL`, `UNKNOWN`), `observed_at` timezone-aware, `source_type` (API oficial / manual / simulado) e `source_reference` segura.
- `affiliate_url` em estrutura **separada** dos preços; não marcar link como pertencente ao usuário só pelo domínio/encurtador.
- `raw_fingerprint` opcional, sem armazenar tokens, cookies ou resposta sensível completa.

**Regras essenciais:**
- `None` é diferente de zero; falta de preço não equivale a produto gratuito.
- Não misturar variações do anúncio (cor/capacidade/voltagem/tamanho) que tenham preços distintos.
- Sempre guardar BRL/currency de forma explícita e usar `Decimal` (não `float`) para cálculos monetários.
- `listed_old_price` somente se efetivamente presente e elegível; histórico próprio fica em coluna/campo separado.
- Não declarar queda se preço antigo ou histórico comparável estiver ausente.
- Disponibilidade incerta, preço condicionado/VIP, informação vencida ou link de afiliado pendente: **não fica elegível para envio real**.
- Deduplicar snapshots usando item + variante + instante/fingerprint, evitando inundar histórico com observações idênticas quando o projeto já possuir regra equivalente.
- Normalizar categoria da loja e passar pelo classificador existente; não sobrescrever correção manual.

## 4. Cupons e ofertas

Modelar `CouponEvidence` com plataforma, código opcional, tipo (`PERCENT`, `FIXED`, `FREE_SHIPPING`, `UNKNOWN`), compra mínima, teto de desconto, datas, elegibilidade, origem, status (`CONFIRMED`, `UNVERIFIED`, `INVALID`) e instante da verificação.

- `CONFIRMED` somente com confirmação técnica suficiente para **produto e condições anunciadas**. Um anúncio de campanha ativa sem validação individual não prova elegibilidade universal; documentar nível de confirmação.
- Preço com cupom apenas quando todos os critérios conhecidos forem satisfeitos, aplicando mínimo/máximo, teto e arredondamento decimal correto. Se condição fundamental for desconhecida, não anunciar valor final garantido.
- Cupom opcional: nenhuma oferta será rejeitada só por não ter cupom.
- Cupom inválido/expirado não deve aumentar score.
- Se loja não expõe consulta de cupons de terceiros, habilitar cadastro manual com status explícito e validação conservadora; **não inventar endpoint**.

## 5. Agendamento, limites e resilência

- Preservar frequência configurável da Fase 2: prioridade a cada ~60 segundos e geral a cada ~300 segundos, condicionada à permissão e ao rate limit da fonte.
- Implementar rate limit por conector/credencial, timeouts, retries exponenciais com jitter, cache e tratamento de `429`/`5xx`; não insistir em `401`/`403` nem tentar contornar bloqueios.
- Monitorar mudança sem substituir o último preço válido por `None` em falha transitória; registrar observação de erro separada.
- Evitar loops que façam chamadas externas em massa após reinício; respeitar locks/leases existentes.
- `PRICE_PROVIDER=simulated` continua funcionando; provedores reais habilitados por flags independentes e explícitas.
- Sem credenciais: startup saudável com fonte real `DISABLED`, sem tentar conectar em background.
- Garantir que fixtures/testes unitários não acionem internet nem serviços de terceiros.

## 6. Afiliados, categorias e fila

- Mercado Livre: publicar futuramente **somente** quando existir link de afiliado manual informado pelo administrador, associado ao item correto e validado operacionalmente.
- Shopee: link de afiliado obtido por função autorizada ou manual, com rastreabilidade; sem link, manter `affiliate_pending`.
- Integrar com categorias existentes (`manual → loja confiável → regras → Gerais`), sem duplicar classificador.
- Preservar modos de destino `unconfigured`, `selected` e `all` e a regra `unconfigured` bloqueia.
- O destino `Ofertas de Casa` continua `disabled`, com categorias Casa e Decoração, Materiais e Ferramentas, Eletrodomésticos, Moda, Infantil, Tecnologia, Games e Beleza (não incluir Gerais automaticamente).
- Fila real apenas em **modo dry-run**: ainda não haverá publisher ativo. Registros precisam informar fonte, verificação, destino elegível, motivo de bloqueio e a hora de próxima elegibilidade.
- Conservar deduplicação e janela mínima de 3 horas, sem criar mais de um item de fila por produto/variante/destino para mesma condição.

## 7. Persistência e rotas administrativas

Reutilizar modelos existentes; criar migração `0004_...` **somente se realmente necessária**, reversível e sem perda de dados. Não sobrescrever a revisão `0003` nem apagar tabelas/volumes.

Adicionar somente as operações administrativas faltantes, com `X-API-Key`, validação e paginação:
- `GET /api/v1/integrations/status` — capacidades, credenciais configuradas **sem revelar segredos**, últimas consultas e estado.
- `POST /api/v1/integrations/{platform}/check` — teste controlado de conexão/permissões, sem divulgação de credenciais.
- `POST /api/v1/products/{id}/refresh-real` — consulta pontual sob demanda, se permitida; falhas retornam status inteligível.
- `GET /api/v1/offers` / rotas existentes — mostrar proveniência, atualização, bloqueios e histórico sem quebrar contratos.
- `GET /api/v1/integrations/{platform}/capabilities` — documentar funções possíveis/indisponíveis.

Se rotas equivalentes já existirem, **estender em vez de duplicar**. Não permitir endpoints externos de administração por padrão; manter bind local e autenticação.

## 8. Segurança

- Variáveis no `.env.example` sem valores reais: flags para Mercado Livre e Shopee, credenciais opcionais e limite de requisições.
- Manter `.env` ignorado pelo Git. Nunca logar cabeçalhos Authorization, `AppId`, `Secret`, cookies, tokens, `TEST_DATABASE_URL` completa ou links com dados secretos.
- Respeitar termos e limites oficiais; evitar chamadas indevidas que possam bloquear a conta de afiliado.
- Em URLs de entrada, prevenir SSRF: validar esquemas `https`, hosts permitidos, redirects e IPs privados/localhost.
- Não fazer solicitações de compra nem ações que alterem campanhas/produtos de vendedores.
- Migrações no banco de desenvolvimento apenas; testes PostgreSQL exclusivamente em `ofertas_bot_test`.

## 9. Estratégia de implementação e testes

**Ordem obrigatória:**
1. Inventariar arquitetura e documentar `docs/INTEGRACOES_FASE_3.md` com a matriz de capacidades de cada loja.
2. Criar contratos e modelos normalizados **com fixtures reais sanitizadas somente quando disponíveis**; usar dados sintéticos de teste identificados como tais.
3. Implementar o caminho Mercado Livre de monitoramento para recursos autorizados + fluxo manual de afiliado.
4. Implementar Shopee com credenciais opcionais e operações autorizadas + fallback manual.
5. Integrar classificação, histórico, score, cupom e fila dry-run.
6. Criar/ajustar migrações e endpoints sem quebrar os existentes.
7. Testar casos negativos e revisar documentação.

**Testes mínimos:**
- Sem credenciais em ambas plataformas; erro legível e nenhuma chamada de rede involuntária.
- Autenticação válida/inválida, timeout, 429, 401, 403, 5xx e schema inesperado (fixtures).
- Preço antigo ausente, preço zero/inválido, desconto apenas percentual arredondado, preço VIP, moeda inesperada e variante com preços distintos.
- Coupon confirmado/não confirmado/expirado; compra mínima e teto; sem cupom.
- Mercado Livre sem afiliado: produto monitorável, **não pronto para publicar**.
- Shopee sem permissão de API: fallback seguro; sem links inventados.
- Categorias múltiplas e manual preservada; `Ofertas de Casa` desativado; fila sem publicação.
- Fila e deduplicação dentro de 3 horas; republicação revalidada depois de 3 horas.
- Falhas externas não destroem histórico anterior.
- `pytest`, Ruff, verificação de formatação, `alembic current/check` e integração PostgreSQL se necessário.

**Teste real supervisionado, se houver acesso:** consultar **um** produto Mercado Livre e **um** Shopee, registrar horário/ID, preço e origem, sem publicar. Se bloqueado, explicar precisamente o motivo e como habilitar, sem alegar êxito.

## 10. Critérios de aceite

A Fase 3 estará concluída tecnicamente quando:
- A integração de cada plataforma tiver capacidades documentadas e estados verdadeiros; sem endpoint suposto.
- Ao menos um fluxo de ingestão real autorizado estiver operacional **ou** as limitações de acesso estiverem registradas de modo explícito, sem fingir que o garimpo real completo foi entregue.
- Preço/variante/origem sejam rastreáveis; sem falsear cupom, preço antigo ou preços VIP.
- Afiliados manuais do Mercado Livre funcionem sem geração automática ilegal/inexistente.
- Categorias e destinos existentes permaneçam corretos; fila só em dry-run; sem mensagens externas.
- Nenhum teste existente regredir e testes novos relevantes passarem.
- README, ROADMAP e documentos de integração ensinarem habilitação por loja, funcionamento local, limites, diagnóstico e próximos passos.

**Atenção:** se nenhuma loja permitir consulta real com as credenciais disponíveis, declarar **FASE 3 PARCIAL / BLOQUEADA POR ACESSO**, não marcar como completa. A Fase 4 depende de revisão/aceite do proprietário.

## 11. Modo de execução do Codex

- Desenvolva autonomamente o escopo definido sem pedir aprovação para passos rotineiros.
- Não peça/mostre senhas em mensagens; sinalize quais variáveis **opcionais** faltam e como o usuário as obtém oficialmente.
- Evite textos longos de progresso e códigos repetidos na resposta. Qualidade e testes têm prioridade sobre economia de tokens.
- Não execute ações destrutivas, gere custos, exponha serviços nem publique ofertas sem aprovação explícita.
- Atualize `ROADMAP.md` e faça commits lógicos; não envie para repositórios remotos sem autorização.
- **Pare ao fim da Fase 3. Não inicie a Fase 4.**

### Formato do relatório final (curto)
1. **Implementado:** capacidades reais de cada plataforma e o que ficou somente mock/manual.
2. **Testes:** aprovados/falhas/ignorados e validação PostgreSQL.
3. **Acessos pendentes:** credenciais/permissões/documentação, sem segredos.
4. **Exemplo real verificado:** um produto por plataforma, somente se consulta real autorizada ocorreu (com timestamp); caso contrário, registrar motivo.
5. **Próximo passo recomendado:** com critérios claros para ativar publicação no futuro.
