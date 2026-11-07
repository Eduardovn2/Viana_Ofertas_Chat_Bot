# Abastecimento por exportação oficial fornecida — 02/11/2026

O caminho utiliza o cadastro manual e a fila/outbox existentes. Nenhum arquivo real
novo foi fornecido ou importado nesta sessão. Não há downloads, feeds, observador
de diretório ou novo agendamento. Cinco candidatos reais continuam pendentes.

## Fontes disponíveis no contrato local

`productOfferV2` fornece identidade, título, preços anunciados/faixas, links e
campos de vendedor. Não comprova preço público aplicável, estoque/opção, titularidade
do link ou cobertura de todas as opções. Os links retornados não viram links próprios
verificados por inferência. A busca preparada permanece limitada aos termos assinados.

`listItemFeeds` permite listar FULL/DELTA e `getItemFeedData` pagina dados por
datafeedId/offset, até 500 registros. O tipo de linha expõe colunas JSON e alterações
NEW/UPDATE/DELETE, mas o contrato local não documenta o dicionário das colunas nem
confirma a permissão da conta. Não há autorização para chamadas adicionais. Uma
consulta futura precisa de escopo/orçamento próprio e validação das colunas reais.

Exportações do portal oficial fornecidas pelo responsável podem abastecer ofertas
**somente se os dados efetivamente presentes comprovarem as condições exigidas**.
Documento do fabricante serve para características/vínculo; não para estoque/preço
de uma loja. Não se presume que uma exportação Shopee contenha todos os campos.

## Operação mais curta

1. Fornecer o arquivo CSV UTF-8 original e referência da origem oficial, conta não
   secreta, data de exportação e dicionário/trecho documental das colunas. Não editar
   a planilha para preencher estados comerciais ausentes. A origem é atestada pelo
   responsável, não autenticada remotamente pelo software.
2. Completar uma cópia de `EXPORTACAO_OFICIAL_MANIFESTO_PENDENTE.json`. Os `null`,
   mapeamento vazio e confirmações falsas são pendências deliberadas, não um exemplo
   de documento real. `columns` relaciona cada campo interno ao cabeçalho exato do CSV:
   `shop_id`, `item_id`, `name`, `original_url`, `public_price`, `availability`,
   `price_access`, `currency`, `observed_at`. `variant_id` é exigido para a modalidade
   VARIANT; não se inventa um identificador quando o documento não o fornece.
   Os valores que comprovam AVAILABLE/PUBLIC devem estar documentados na referência
   de semântica. UNKNOWN não pode ser mapeado a condição comprovada.
3. Produto e link próprio verificado devem já existir. Identidade documental válida
   é reutilizada do histórico sem confirmação repetida. Um documento novo pode ser
   incluído em `identity_documents`, por ID local, mas exige confirmação explícita
   em `confirmed_new_identity_documents`. O importador não cadastra produtos nem links.
4. Executar a prévia local (o comando calcula o hash dos bytes; sem revisão de hash):

   ```powershell
   .venv/Scripts/python.exe -m app.import_official_offers --csv CAMINHO_DO_CSV --manifest MANIFESTO_PREENCHIDO.json
   ```

   API autenticada: `POST /api/v1/manual-offers/official-exports/preview`.
   A prévia não cria oferta, observação ou confirmação. Informa linhas READY/BLOCKED
   e os motivos. A API recebe `csv_content` e seu SHA-256, calculados pelo comando.
5. Após confirmar origem, semântica e autorização comercial do lote, usar o mesmo
   comando com `--commit`. API: `POST /api/v1/manual-offers/official-exports/import`.
   Os três campos correspondentes precisam estar explicitamente verdadeiros no
   manifesto; o comando não os preenche. Isso autoriza cadastrar as linhas válidas,
   sem aprovar/iniciar piloto ou publicar mensagens.

## Validação e atualização

- Máximo de 200 linhas por arquivo, cabeçalhos únicos, colunas reais, decimal sem
  agrupamento/faixa ambígua, BRL, IDs e URL do mesmo anúncio, opção documentada.
- A data de observação vem da linha, com fuso. Deve ser atual (menos de 600 segundos),
  não posterior à exportação nem ao relógio. Exportar novamente um dado antigo não
  renova a validade. A cadência de arquivos precisa sustentar esse limite.
- VARIANT reutiliza fatos de identidade válidos; preço e disponibilidade vêm da
  linha atual. LISTING_ALL_OPTIONS precisa também das colunas explícitas
  `all_options_same_public_terms` e `all_options_available`, com valores documentados.
  Só essas novas evidências podem renovar a cobertura e o preço de todas as opções.
- Sem link próprio verificado, disponibilidade, preço público ou identidade, a linha
  fica BLOCKED. Não há fallback que fabrique verificação ou copie preço histórico.
- Arquivo/hash, referência, conta, semântica, registro, data e responsável ficam
  vinculados à evidência. Importação acrescenta observações e ofertas pelo serviço
  existente; não reescreve históricos anteriores.
- Reimportação do mesmo arquivo é idempotente, inclusive concorrente. Outro
  mapeamento para o mesmo arquivo é recusado. Repetir arquivo vencido não o atualiza.
- Exportação confirmada mais recente com preço inconclusivo, acesso desconhecido ou
  indisponibilidade bloqueia a oferta importada anterior por um vínculo de validade
  separado, sem criar uma oferta ou observação comercial falsa. Documentos mais
  antigos não substituem a observação confirmada mais recente.
- O piloto ativo consome ofertas importadas verificadas pela mesma fila, com termos
  do plano, validade, cooldown, reservas, orçamento e outbox. Não exige nova busca
  Shopee ou aprovação por mensagem. Cada reserva e envio revalida as evidências.

O plano existente continua PREPARED, sem mudança de UUID, hash, termos, orçamento ou
janela (03/11/2026 09h–11h Brasília). Não foi aprovado nem iniciado. Zero chamadas
externas, novos links ou mensagens. Até chegar documentação comercial suficiente,
o resultado continua **zero ofertas reais elegíveis**.

Validação: 32 testes específicos offline e 16 PostgreSQL isolados do importador
aprovados, incluindo API autenticada, atualização negativa, idempotência concorrente,
VARIANT/LISTING_ALL_OPTIONS e consumo pela fila sem consulta Shopee. Regressão de
147 testes offline também aprovada (contagens sobrepostas); Ruff aprovado.
Endpoints carregados no backend assistido em 127.0.0.1:8000. Conferência final do
plano persistido: PREPARED, sem aprovação, consultas ou mensagens reservadas;
destino pausado e scheduler desligado.
