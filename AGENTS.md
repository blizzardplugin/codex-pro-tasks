# Codex Pro Tasks

Este projeto coordena agentes locais autenticados pelo login oficial do ChatGPT no Codex CLI. Não usa chaves de API e não controla a interface do ChatGPT por automação de navegador.

- Credenciais ChatGPT permanecem no dispositivo/usuário de sistema de cada agente. Nunca leia, copie ou publique `auth.json`, senhas ou tokens dessas contas.
- Não altere HOME/CODEX_HOME para alternar contas. Cada agente usa seu ambiente próprio e seu login normal.
- Não contorne limites. Ao detectar limite de uso, pause esse agente e mantenha a tarefa vinculada a ele; retomada é explícita, após a recuperação do limite.
- Cada tentativa usa checkout e branch próprios. Não modifique nem faça merge automático na branch principal dos projetos atendidos.
- Comandos de verificação vêm da configuração autorizada do projeto, como listas de argumentos, nunca como comandos shell extraídos da resposta do agente.
- Teste a fila, dependências, leases, autenticação, isolamento e publicação. Diferencie testes com CLI simulada de uma execução real autenticada pela assinatura.
- Atualize README e documentação operacional quando o comportamento mudar. Não afirme que agentes reais estão conectados sem evidência.
