[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8000',
    [switch]$Cleanup
)

$ErrorActionPreference = 'Stop'

# Dados fornecidos no briefing. Não representam uma oferta verificada.
# A API deve estar em execução com PRICE_PROVIDER=simulated.
$apiRoot = $BaseUrl.TrimEnd('/') + '/api/v1'
$secureApiKey = Read-Host 'Informe ADMIN_API_KEY do seu .env' -AsSecureString
$requestHeaders = @{
    'X-API-Key' = [System.Net.NetworkCredential]::new('', $secureApiKey).Password
}
Remove-Variable secureApiKey
$createdProductId = $null

try {
    $health = Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + '/health')
    Write-Host 'Saude da API:'
    $health | ConvertTo-Json -Depth 5 | Write-Output

    $productBody = @{
        platform = 'MERCADO_LIVRE'
        name = 'Kit 7 pecas cabeceira modular - exemplo do briefing'
        original_url = 'https://produto.mercadolivre.com.br/MLB-3810190843-kit-7-pecas-cabeceira-de-cama-modular-para-cama-box-de-casal-_JM'
        monitoring_enabled = $true
        priority = $false
    } | ConvertTo-Json

    $product = Invoke-RestMethod -Method Post -Uri "$apiRoot/products" -Headers $requestHeaders -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($productBody))
    $createdProductId = $product.id
    Write-Host "Produto de exemplo criado: $createdProductId"

    $affiliateBody = @{ url = 'https://meli.la/1JQSATy' } | ConvertTo-Json
    $null = Invoke-RestMethod -Method Put -Uri "$apiRoot/products/$createdProductId/affiliate-link" -Headers $requestHeaders -ContentType 'application/json' -Body $affiliateBody

    $null = Invoke-RestMethod -Method Post -Uri "$apiRoot/products/$createdProductId/monitor" -Headers $requestHeaders
    $null = Invoke-RestMethod -Method Post -Uri "$apiRoot/products/$createdProductId/monitor" -Headers $requestHeaders

    Write-Host 'Historico SIMULADO (nenhuma loja foi consultada):'
    Invoke-RestMethod -Uri "$apiRoot/products/$createdProductId/price-history" -Headers $requestHeaders | ConvertTo-Json -Depth 10 | Write-Output

    if (-not $Cleanup) {
        Write-Host "Cadastro preservado para consulta em $apiRoot/products/$createdProductId"
    }
}
finally {
    try {
        if ($Cleanup -and $null -ne $createdProductId) {
            $null = Invoke-RestMethod -Method Delete -Uri "$apiRoot/products/$createdProductId" -Headers $requestHeaders
            Write-Host "Produto de exemplo excluido: $createdProductId"
        }
    }
    finally {
        Remove-Variable requestHeaders -ErrorAction SilentlyContinue
    }
}
