# Validacao de link manual Shopee - Viana Ofertas

## Resultado

Formato aceito pelo validador existente. Link original preservado, sem normalizacao/substituicao:

https://s.shopee.com.br/30ox0iCXhm

Foi realizada **uma requisicao HEAD**, sem autenticacao, cookies, retries ou seguimento de redirecionamentos. Resposta **HTTP 301**, com Location HTTPS em dominio canonico permitido da Shopee. A verificacao limitada nao estabeleceu shop_id/item_id do produto com seguranca. Nenhuma pagina de produto foi carregada e nenhum rastreador do redirecionamento foi exibido ou armazenado.

Um redirecionamento oficial nao comprova a titularidade do afiliado, a identidade/variante, disponibilidade, preco ou condicoes comerciais. A origem Central de Criadores foi informada pelo responsavel, nao certificada automaticamente.

## Servicos existentes inspecionados

- app/services/products.py: validate_affiliate_url aceita o host exato s.shopee.com.br via HTTPS e retorna a URL sem alteracao.
- app/services/affiliate_links.py: validate_link_for_product verifica IDs em URLs reconheciveis; um link curto opaco depende de verificacao humana e nao comprova a associacao.
- app/api/products.py: PUT /api/v1/products/{product_id}/affiliate-link, administrativo, recebe url e operationally_verified. Sem confirmacao utiliza MANUAL_SHOPEE; com confirmacao operacional explicita utiliza MANUAL_SHOPEE_VERIFIED. Nao gera outro link nem consulta a plataforma.
- app/services/manual_offers.py e app/services/offers.py: oferta manual/publicabilidade exigem link proprio verificado e evidencias comerciais separadas; salvar o link nao autoriza publicacao.

## Cadastro e pendencia

E possivel usar exatamente o link fornecido, sem generateShortLink. Entretanto AffiliateLink exige product_id e nao oferece cadastro avulso sem produto. **Nao foi efetuado cadastro nem marcada verificacao operacional**, pois o produto correspondente nao foi confirmado.

O banco possui um produto Shopee local id 1: cabo HDMI. Ele nao foi associado ao link informado, nao recebeu qualquer alteracao e continua sem link de afiliado. A mera existencia desse produto nao permite supor correspondencia.

Para concluir a associacao, o responsavel deve informar a URL canonica completa ou os IDs publicos loja/item do anuncio de destino, a variante quando aplicavel, e confirmar que esse link proprio foi gerado em sua conta para exatamente esse anuncio. Nome isolado nao comprova identidade. Nao enviar credenciais ou rastreadores extras. Se for outro produto, utilizar seu cadastro correto em vez de substituir/vincular ao cabo existente.

Depois dessa confirmacao, o endpoint existente permite persistir a URL original; operacionalmente verificado somente apos a afirmacao explicita de correspondencia/titularidade. Preco, estoque, cupom e aprovacao de publicacao continuam separados.

## Verificacoes executadas

- Validacao local do link real: formato valido, URL original inalterada.
- 12 testes existentes de validacao/preservacao de links aprovados; 54 casos nao selecionados nesse comando (nao sao falhas).
- Consulta somente de leitura ao PostgreSQL: link ainda nao cadastrado, produto existente nao alterado.
- Cinco flags false: SHOPEE_ENABLED, MERCADO_LIVRE_ENABLED, TELEGRAM_PUBLISHER_ENABLED, TELEGRAM_REAL_SENDS_AUTHORIZED e SIMULATION_AUTOMATION_ENABLED.
- Grupo Viana Ofertas pausado; monitoramento principal desativado.
- 1 HEAD de validacao; 0 consultas a APIs comerciais; 0 generateShortLink; 0 publicacoes Telegram; 0 gravacoes no banco.

Nenhuma funcionalidade, credencial ou configuracao foi alterada. Sem commit.
