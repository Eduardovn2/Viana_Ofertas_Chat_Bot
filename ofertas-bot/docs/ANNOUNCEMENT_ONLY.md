# Publicação informativa explícita

## Visual profissional PRO_V2

Novos planos informativos sem mensagem previamente fixada usam automaticamente
`announcement_template: PRO_V2`. O campo faz parte do hash do escopo.
Planos antigos sem esse campo e mensagens com hash já fixado mantêm LEGACY_V1.
Não há edição de mensagens enviadas nem migração de autorizações.

O template apresenta título curto atribuído ao anúncio e preserva o título
completo abaixo. Atributos comprovados do comparador podem produzir o título
documental; pistas textuais não entram como fatos comprovados. Usa preço
**anunciado**, link explícito, ressalva de condições e identificação publicitária.
Opções não são deduzidas dos títulos. Avaliação/vendas entram apenas de métricas
documentadas, com identidade e horário válidos; escala desconhecida é omitida e
período desconhecido permanece identificado como tal. Descontos históricos não
entram neste template; VERIFIED_OFFER conserva seu template separado.

Imagem da própria captura oficial, em CDN admitida pelo conector, usa sendPhoto.
O HTTP guard verifica método, imagem e legenda exatos antes de consumir o POST.
Sem imagem permitida usa sendMessage. Legendas acima de 1024 unidades UTF-16
visíveis viram uma única mensagem de texto antes do HTTP, sem tentativa inicial
de foto. Texto acima de 4096 é recusado. Dados e URLs são escapados/validados.

Prévia local, somente leitura, sem autorizar envio ou renovar a cotação:

```powershell
.venv/Scripts/python.exe -m app.preview_announcement --pilot-id 76350f50-29a2-4d68-85a0-fa74c0da765b --identity 1018042820:22293101052 --output .runtime/previews/hdmi-pro-v2
```

Produz `preview.html`, `message.txt` e `message.json` com o mesmo renderizador
utilizado na outbox. O HTML aproxima o Telegram; fontes/largura variam por cliente.
Não baixa a imagem; a prévia permite abri-la mediante interação do usuário.

Novos planos podem usar `commercial_mode: ANNOUNCEMENT_ONLY`. Ausência do campo
ou `VERIFIED_OFFER` mantém as regras anteriores, inclusive disponibilidade e
preço aplicável. Nenhuma autorização anterior é ampliada.

O modo informativo exige captura oficial bem-sucedida no próprio piloto, preço
anunciado BRL válido com menos de 600 segundos, identidade do anúncio, URL e
recibo assinado do link próprio com prova de conta vigente. Links legados sem
recibo são recusados. Não afirma seleção de opção, kit, frete, estoque, preço
final, desconto histórico ou menor preço. O título aparece identificado como
título do anúncio, não como validação de todas as afirmações do vendedor.

`announcement_identities` restringe os IDs permitidos no escopo. A descoberta
continua pelos termos assinados; resultado fora da lista não é publicado.
O modo não gera links: candidatos sem vínculo verificável ficam bloqueados.
Geração real continua exigindo autorização separada pelo serviço existente.

## Mesmo fluxo, política diferente

O AUTO_PILOT consome as capturas da descoberta, valida link e atualidade e cria
uma oferta informativa, sem ManualOfferDetails nem confirmação comercial falsa.
A observação real é anexada com fonte `shopee:announcement_only`, preço anunciado,
availability UNKNOWN e price_access UNKNOWN, sem alterar observações antigas.
Ela não é promovida a prova comercial nem usada como desconto histórico.

Usa fila, outbox, aprovação pelas regras assinadas do piloto, reservas antes do
HTTP, leases, cooldown, cadência e publicador existentes. Revalida captura, preço,
recibo, conta, destino, modo e autorização antes do envio. O publicador comercial
padrão continua recusando esses registros como ofertas verificadas.
UNCERTAIN não é repetido automaticamente. Um bloqueio não para outros candidatos.

## Operação

Preparar plano novo com `commercial_mode`, identidade permitida, janela, termos,
orçamentos e destino. A aprovação administrativa existente vincula o hash completo.
O responsável precisa autorizar expressamente **publicação informativa**, incluindo
a consulta externa se for necessária atualização. Não iniciar planos antigos.

Após aprovação pelo endpoint existente, iniciar o piloto autorizado e rodar:

```powershell
.venv/Scripts/python.exe -m app.auto_pilot_worker --pilot-id NOVO_UUID --scope-sha256 NOVO_HASH
```

O worker não precisa de IA. Encerramento e restauração do destino continuam nos
controles existentes. Sem candidatos válidos ou orçamento, nenhum envio ocorre.

Para feeds, `python -m app.prepare_feed_probe` somente prepara uma nova autorização
de uma consulta `listItemFeeds`, com prazo, credencial, prova de conta e payload
vinculados. Não executa, não aprova e não lê linhas. Se houver feed acessível,
`getItemFeedData(offset:0,limit:1)` precisa de autorização independente vinculada
ao datafeedId realmente retornado; não se presume conteúdo nem permissão.
