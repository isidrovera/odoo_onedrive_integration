# Microsoft 365 — OneDrive y SharePoint para Odoo 19

Extensión del módulo original `odoo_onedrive_integration`. Conserva el modelo
`onedrive.account` y el cliente OWL, y agrega administración centralizada de:

- OneDrive personal y bibliotecas de documentos SharePoint.
- Usuarios invitados de Microsoft Entra B2B.
- Grupos de seguridad y grupos Microsoft 365, con miembros y propietarios.
- Sitios SharePoint, bibliotecas y ubicaciones documentales autorizadas.
- Roles Odoo por ubicación: lector, editor y gestor.
- Conexión Microsoft individual opcional y auditoría de operaciones en Odoo.

## Autenticación y contraseña

La contraseña de Odoo nunca se copia a Microsoft y Microsoft no permite que una
aplicación conozca o cree la contraseña de un invitado.

El administrador selecciona los usuarios en **Microsoft 365 > Administración >
Aprovisionar usuarios**. Odoo crea o vincula la identidad invitada, envía la
invitación y aplica grupos/ubicaciones. El usuario solo debe aceptar la invitación
de Microsoft una vez.

Hay dos modos de operación:

1. **Aplicación administrativa (sin conexión Microsoft individual):** el usuario
   trabaja dentro de Odoo sin escribir una contraseña Microsoft. Odoo registra al
   usuario real en `Auditoría`; en SharePoint, `modifiedBy` puede aparecer como la
   aplicación o cuenta de servicio.
2. **Identidad delegada (atribución Microsoft):** cada usuario pulsa una vez
   **Mi conexión > Conectar**. Si ya tiene sesión Microsoft, normalmente se usa
   SSO; Microsoft puede pedir MFA, OTP o autenticación según las políticas del
   tenant. Los refresh tokens evitan pedir la contraseña en cada operación. En
   este modo SharePoint/OneDrive puede registrar al usuario Microsoft real como
   autor de la modificación.

Para abrir su OneDrive personal, cada usuario debe usar la conexión delegada. En
SharePoint puede trabajar directamente con la aplicación administrativa si no
necesita atribución individual dentro de Microsoft.

No es técnicamente posible combinar “cero autenticación Microsoft del usuario”
con “`modifiedBy` siempre igual al usuario” usando permisos de aplicación. La
auditoría Odoo sí conserva el usuario en ambos modos.

## Registro de aplicación en Microsoft Entra

1. Cree una aplicación de tenant único.
2. Agregue como URI de redirección web:
   `https://SU_ODOO/microsoft/oauth/callback`.
3. Cree un secreto de cliente.
4. Conceda consentimiento administrativo a los permisos de **aplicación** que
   realmente utilizará:
   - `User.Invite.All` para invitados.
   - `User.Read.All` para localizar usuarios y asignarlos al crear grupos.
   - `Group.ReadWrite.All` para crear/sincronizar grupos y membresías.
   - `Sites.ReadWrite.All` para sitios, bibliotecas y documentos SharePoint.
   - `Files.ReadWrite.All` si administrará unidades/archivos con Graph.
5. Para crear sitios modernos mediante SharePoint REST, conceda además el permiso
   de aplicación SharePoint `Sites.FullControl.All`. Si la política del tenant no
   lo permite, cree el sitio fuera de Odoo y use **Sincronizar grupos y sitios**.
6. Mantenga los permisos delegados configurados en
   `microsoft365.oauth.delegated_scope` para las conexiones personales. El valor
   incluido es:
   `openid profile email offline_access User.Read Files.ReadWrite.All Sites.ReadWrite.All`.

Use el menor conjunto de permisos compatible con su caso y revise las políticas
de uso compartido externo, MFA, acceso condicional y OTP del tenant.

## Puesta en marcha

1. Haga una copia de seguridad y pruebe primero en una base clonada.
2. Reemplace la carpeta anterior manteniendo el nombre técnico
   `odoo_onedrive_integration`.
3. Reinicie Odoo, actualice la lista de aplicaciones y actualice el módulo.
4. Asigne **Microsoft 365 / Administrador Microsoft 365** al administrador.
5. Configure Tenant ID, Client ID, secreto y URL raíz SharePoint.
6. Pulse **Probar aplicación**, **Preparar OneDrive** y luego
   **Sincronizar grupos y sitios**.
7. Revise/cree grupos, sitios y bibliotecas.
8. En **Ubicaciones y permisos**, defina lectores, editores y gestores.
9. Use **Aprovisionar usuarios** para crear invitados y aplicar sus accesos.

## Roles de la interfaz documental

| Rol | Operaciones |
|---|---|
| Lector | listar, buscar, previsualizar y descargar |
| Editor | lector + subir, crear carpetas, renombrar, mover y copiar |
| Gestor | editor + compartir y enviar a papelera |

Los controladores vuelven a validar el permiso en servidor; ocultar botones en la
interfaz no es la única protección.

## Notas operativas

- Los secretos y tokens solo son visibles para el grupo administrador. Proteja y
  cifre las copias de seguridad de PostgreSQL y limite el acceso al servidor.
- La membresía no es destructiva por defecto. Active **Odoo controla toda la
  membresía** únicamente si desea retirar miembros remotos que no estén en Odoo.
- La invitación B2B crea la identidad inmediatamente, pero Microsoft exige el
  proceso de canje antes de que el invitado pueda acceder a recursos.
- La creación de grupos/sitios puede ser asíncrona en Microsoft; use
  **Comprobar / sincronizar** si el sitio continúa en aprovisionamiento.
- La descarga ZIP de carpetas no está habilitada en esta versión.

## Validación incluida

El paquete fue comprobado con compilación Python, parseo XML/CSV y revisión de
referencias de archivos. La validación final debe realizarse instalándolo en una
base de prueba Odoo 19 conectada a un tenant Microsoft de pruebas.
