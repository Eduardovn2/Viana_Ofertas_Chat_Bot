# Coletor comercial Python

O worker existente chama `collect_commercial_evidence` em cada tick ativo, antes
da fila e depois da descoberta. Não depende de IA, navegador ou CAPTCHA.
O adaptador implementado é **CONFIRMED_EXPORT_DIRECTORY_V1**, uma entrada local
para exportações oficiais confirmadas, não uma conexão de rede ao feed Shopee.

## Configuração em um novo escopo

Adicionar ao plano antes de calcular o hash e obter autorização:

```json
{
  "commercial_collection": {
    "source": "CONFIRMED_EXPORT_DIRECTORY_V1",
    "directory": "C:/dados/vianinha/commercial-inbox",
    "account_reference": "REFERENCIA_REAL_DA_CONTA",
    "source_references": ["REFERENCIA_REAL_DA_EXPORTACAO_AUTORIZADA"],
    "max_documents": 20,
    "max_documents_per_tick": 3,
    "poll_interval_seconds": 30
  }
}
```

Não adicionar isso a autorizações antigas. Sem o campo, nenhum coletor é ativado.
Manter a pasta privada e gravável somente pelo produtor autorizado e operador.
O hash identifica os bytes recebidos; não é assinatura da Shopee nem prova de origem.
Arquivos locais não recebem verificação verdadeira automaticamente: o manifesto
deve trazer confirmações reais, referência de origem e dicionário de colunas.

## Alimentação sem IA

O produtor autorizado entrega CSV e manifesto do importador existente:

```powershell
.venv/Scripts/python.exe -m app.import_official_offers --csv C:/dados/export.csv --manifest C:/dados/manifest.json --inbox C:/dados/vianinha/commercial-inbox
.venv/Scripts/python.exe -m app.auto_pilot_worker --pilot-id UUID_AUTORIZADO --scope-sha256 HASH_AUTORIZADO
```

O primeiro comando faz somente entrega local atômica; não chama API nem confirma
campos pelo operador. O segundo mantém as autorizações e transportes do worker.
Ele não aprova nem inicia um plano PREPARED.

Campos comerciais aceitos: shop_id, item_id, variant_id quando documentado, nome,
URL original, preço público, disponibilidade, acesso ao preço, BRL, horário e,
para LISTING_ALL_OPTIONS, condições explícitas de todas as opções. Os dados devem
ter menos de 600 segundos. Não se renova o horário ao reler um arquivo.
Documentação de identidade e link próprio válidos são reutilizados. Identidade
desconhecida, produto ainda não cadastrado ou link ausente ficam bloqueados;
o coletor não inventa registros nem gera links. Candidatos continuam na análise
de descoberta mesmo quando não podem virar oferta.

O relatório administrativo existente inclui `commercial_collection.documents`,
com fingerprints, horários, resultados por linha e IDs das ofertas criadas.
Cada manifesto é contado uma vez por piloto. Importação e diário são persistidos
na mesma transação, sob bloqueio da linha do piloto; reiniciar não renova orçamento.
Um arquivo bloqueado não interrompe os demais. Um produtor pode registrar
`{"source_state":"CAPTCHA"}` sem que isso gere evidência ou nova tentativa HTTP.
Arquivos sem confirmação são bloqueados, fontes fora do escopo são recusadas.
O limite por documento continua sendo 200 linhas do importador existente.

## Fonte online ainda necessária

O contrato local de productOfferV2 oferece preço anunciado, título, IDs, URLs,
imagem e indicadores de produto, mas não comprova estoque, opção selecionada ou
preço público aplicável. shopOfferV2 tampouco resolve esses campos.
listItemFeeds/getItemFeedData estão documentados, mas `columns` é JSON sem
dicionário local: não há prova de acesso da conta nem de que forneça esses campos.

Para um produtor online contínuo falta **permissão oficial da conta para o feed,
um exemplo real autorizado e o dicionário oficial das colunas**, demonstrando
disponibilidade, preço aplicável, opções e data de atualização, além do orçamento
de chamadas aprovado. Se o feed não fornecer esses dados, é necessária outra
fonte oficial com esse contrato. Não basta autorizar mais productOfferV2.
Nenhuma coleta remota ou publicação foi habilitada por esta implementação.
