# Prova de conta reutilizável

O cadastro administrativo existente recebe uma prova por conta/credencial em
`POST /api/v1/integrations/shopee/account-evidence`, com autenticação `X-API-Key`.
Não há prova registrada por esta preparação.

A referência oficial deve identificar a conta afiliada autorizada e seu vínculo
ao App ID configurado. O App ID é comparado internamente; nunca fornecer secret,
token ou senha. Registrar origem, data da conferência, validade e responsável.
O SHA-256 do documento é calculado pelo sistema, sem confirmação humana do hash.
As duas confirmações de titularidade e vínculo da credencial são explícitas,
somente após a conferência real; o endpoint não as presume.

Para o link legado do produto 2, o mesmo conjunto documental também precisa
associar `https://s.shopee.com.br/1qczeLcPMR` à conta. Seu recibo de geração não
guardou fingerprint da credencial. A continuidade do `.env` não supre essa
lacuna. Incluir seu hash em `covered_legacy_link_sha256` somente se efetivamente
coberto pelo documento. Não reescrever nem completar retroativamente o recibo.

Consultar a prova vigente em
`GET /api/v1/integrations/shopee/account-evidence/current`. Ela é vinculada à
credencial por fingerprint e assinatura; vencimento, alteração do documento
persistido ou troca da credencial impedem seu reaproveitamento. Um recibo futuro
com vínculo da mesma credencial pode reutilizar a prova sem nova conferência de
conta. Recibos sem esse vínculo continuam exigindo cobertura documental explícita.

## Documento único e cadastro local

Fornecer a tela oficial do portal Affiliate que exibe o **App ID / credencial da
Open API**, junto à identificação da conta afiliada/beneficiário. O segredo deve
estar oculto. Se o portal não mostrar ambos na mesma tela, fornecer o perfil da
conta e a tela do App ID na mesma sessão como um único conjunto documental.
Não presumir nomes de menus nem acesso a exportações não documentadas.

Após a revisão real, preencher um manifesto local com os campos de
`AccountEvidenceInput`: `actor`, `account_reference`, `documented_app_id` lido do
documento, `reference` (`document:...`), `verified_at`, `expires_at`,
`confirmed_account_ownership=true`, `confirmed_credential_binding=true`.
`covered_legacy_link_sha256` é opcional e somente para links efetivamente cobertos
pelo documento. A data de conferência inclui fuso; validade máxima de um ano.
Não criar manifesto confirmado antes de receber e conferir a prova real.

Da raiz do repositório, usando caminhos reais do documento e do manifesto:

```powershell
.venv\Scripts\python.exe -m scripts.register_shopee_account_evidence --document "C:\caminho\prova-oficial.pdf" --manifest "C:\caminho\manifesto-conta.json"
.venv\Scripts\python.exe -m scripts.register_shopee_account_evidence --document "C:\caminho\prova-oficial.pdf" --manifest "C:\caminho\manifesto-conta.json" --register
```

O primeiro comando valida sem cadastrar. O segundo usa o endpoint administrativo
local, calcula o hash do arquivo, compara o App ID documentado e lê a chave
administrativa da configuração sem imprimi-la. Nenhum comando acessa a Shopee.

## Novos links e finalização comercial

Reutiliza `OfficialExportInput`, suas confirmações existentes de origem/semântica,
os documentos de identidade já válidos e `process_export`. Não infere preço
público ou estoque da busca Affiliate. Sem documento comercial atual, não há lote
elegível, ainda que a conta esteja comprovada.

Endpoints administrativos, todos com `X-API-Key`:

1. `POST /api/v1/integrations/shopee/link-batches`: preparar UUID novo, `actor`,
   `not_before`, `expires_at`, `export` (manifesto oficial já suportado). Até dez
   anúncios documentados, uma tentativa por anúncio. A janela não ultrapassa os
   600 segundos da observação comercial.
2. `GET /api/v1/integrations/shopee/link-batches/{uuid}`: revisar hash, origem,
   conta/credencial vinculadas, candidatos, limite e prazo.
3. Somente após autorização específica para geração e cadastro do lote:
   `POST .../{uuid}/approve`, com `scope_sha256`, `actor` e
   `confirmed_link_generation=true`; depois `POST .../{uuid}/execute` uma vez.

A reserva é persistida antes do HTTP. Timeout, encerramento durante a tentativa
ou resultado não conciliado nunca liberam repetição automática. Cada recibo novo
guarda fingerprint da credencial, referência à prova vigente, identidade/opção,
payload, origem, horário e assinatura. A associação exige **prova válida mais
geração autenticada com identidade correspondente**, não somente HTTP 200.
O fluxo não afirma que o link seleciona automaticamente a variante.

Depois da geração, a importação existente cria ofertas apenas com os dados
comerciais confirmados e vigentes. O AUTO_PILOT consome esses registros pelo
`enqueue_documented_imports` já existente, sem aprovação por mensagem. O recibo e
a prova da conta são revalidados antes do consumo/publicação. `CORE_ATTRIBUTES_ONLY`
continua textual e com o aviso obrigatório. A autorização deste lote não autoriza
Telegram nem amplia orçamento dos pilotos existentes, cujo escopo permite zero
novos links. Não há geração automática habilitada nesta preparação.

Consultar a situação do link preservado em
`GET /api/v1/products/2/shopee/preserved-link-readiness?generation_reference=shopee_shortlink_once:50f837480acf995acb3a4eabd3901a113407042b16177d9fdf1a80442e95d9f9`.
Essa consulta não associa links, não verifica variantes e não autoriza publicação.

# Produto 2: mensagem limitada

O rascunho propõe `message_scope=CORE_ATTRIBUTES_ONLY`, no mesmo fluxo de ofertas
manuais. Exige nome documentado e vinculado por hash, não aceita composição
preenchida como comprovada, omite imagens e inclui obrigatoriamente:

> Volante, pedais, câmbio e demais equipamentos: composição não confirmada. Confira os itens incluídos no anúncio.

Não há afirmação de inclusão nem de exclusão. Não autoriza desconto histórico
nem equivalência entre lojas. As modalidades existentes continuam obrigatórias:
`VARIANT` com identidade de variante documentada, ou `LISTING_ALL_OPTIONS` com
preço público e disponibilidade comprovados para todas as opções. Não inventar
SKU para transformar a declaração AZUL em identificador oficial.

Depois da prova estável da conta/opção, conferir preço público aplicável e
disponibilidade, registrar a observação real e usar o limite de 600 segundos.
Os valores antigos não são confirmação atual. Cadastro, associação operacional
do link e despacho continuam sujeitos às autorizações existentes.
