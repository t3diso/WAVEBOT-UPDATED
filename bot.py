import asyncio
import datetime
import json
import os
import random
import re
import discord
from discord import app_commands
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True

DURACION_REGEX = re.compile(r"^(\d+)([hmsHMS])$")

LINKS_BANEADOS_PATH = "linkban_canal.json"
linkban_canal = set()

GIVEAWAYS_PATH = "giveaways.json"
giveaways_db = {}

IPOV_REGEX = re.compile(r"^\d+\.\d+\.\d+\.\d+(?:/\d+)?$")

WARNS_PATH = "warns.json"
warns_db = {}

LOGS_CHANNELS_PATH = "logs_channels.json"
logs_channels = set()

HONEYPOTS_PATH = "honeypots.json"
honeypots_db = {}   # guild_id (str) -> {channel_id (str): {"action": "ban|kick|mute", "duration": int|None}}

PREFIXES_PATH = "prefixes.json"
prefixes_db = {}     # guild_id (str) -> list[str] de prefijos válidos
REMINDERS_PATH = "reminders.json"
reminders_db = {}    # id -> {user_id, channel_id, msg_id, msg, fin, md}
DEFAULT_PREFIX = "."

MENTION_REGEX = re.compile(r"^<@!?(?P<id>\d+)>\s*$")


def _get_prefixes_sync(guild_id):
    """Devuelve la lista de prefijos válidos para un guild, siempre incluye el DEFAULT_PREFIX."""
    customs = prefixes_db.get(str(guild_id), [])
    return list({DEFAULT_PREFIX, *customs})


def get_prefix_message(guild):
    if guild is None:
        return DEFAULT_PREFIX
    prefs = _get_prefixes_sync(guild.id)
    return " | ".join(f"`{p}`" for p in prefs)


async def _determinar_prefijo(bot_obj, message):
    """Función callable usada por commands.Bot(command_prefix=...)."""
    if not isinstance(message, discord.Message) or message.guild is None:
        return commands.when_mentioned_or(DEFAULT_PREFIX)(bot_obj, message)
    customs = _get_prefixes_sync(message.guild.id)
    return commands.when_mentioned_or(*customs)(bot_obj, message)


bot = commands.Bot(command_prefix=_determinar_prefijo, intents=intents, help_command=None)


def parsear_duracion(texto: str):
    """
    Convierte una cadena tipo '5h', '30m', '10s' o combinaciones a segundos.
    Acepta combinaciones como '1h30m', '2h15m30s'.
    Devuelve (segundos_totales, None) o (None, mensaje_error).
    """
    if not texto:
        return None, "Debes indicar una duración (ej: 5h, 30m, 10s)."

    match = re.findall(r"(\d+)([hmsHMS])", texto)
    if not match or "".join(d + u for d, u in match) != texto.lower():
        return None, f"Formato de duración inválido: `{texto}`. Usa combinaciones de h/m/s (ej: `5h`, `1h30m`, `10s`)."

    total = 0
    for cantidad, unidad in match:
        cantidad = int(cantidad)
        unidad = unidad.lower()
        if unidad == "h":
            total += cantidad * 3600
        elif unidad == "m":
            total += cantidad * 60
        elif unidad == "s":
            total += cantidad
    return total, None


async def resolver_miembro(guild: discord.Guild, user_id: int):
    """Intenta obtener el miembro del servidor; si no está, devuelve el usuario."""
    miembro = guild.get_member(user_id)
    if miembro is not None:
        return miembro, False
    try:
        usuario = await bot.fetch_user(user_id)
    except discord.NotFound:
        return None, None
    return usuario, True


async def resolver_usuario(guild: discord.Guild, texto: str):
    """
    Acepta ID, mención <@123>, o nombre de usuario, y devuelve (usuario, miembro, error).
    - usuario: discord.User (siempre que se encuentre).
    - miembro: discord.Member si está en el guild, sino None.
    - error: mensaje str si no se encuentra.
    """
    if texto is None or not texto.strip():
        return None, None, "❌ Debes indicar un usuario (ID, @mención o nombre)."
    texto = texto.strip()

    m = MENTION_REGEX.match(texto)
    if m:
        try:
            uid = int(m.group("id"))
        except ValueError:
            return None, None, "❌ Mención inválida."
    else:
        try:
            uid = int(texto)
        except ValueError:
            if guild is not None:
                for mb in guild.members:
                    if mb.name.lower() == texto.lower() or (getattr(mb, "global_name", None) and mb.global_name.lower() == texto.lower()):
                        return mb, mb, None
                return None, None, f"❌ No encontré a ningún usuario llamado `{texto}` en este servidor. Prueba con su ID o @mención."
            return None, None, "❌ Sin guild no puedo resolver por nombre. Usa ID o mención."

    miembro = guild.get_member(uid) if guild is not None else None
    try:
        usuario = await bot.fetch_user(uid)
    except discord.NotFound:
        return None, miembro, f"❌ No se encontró ningún usuario con ID `{uid}`."
    except discord.HTTPException as e:
        return None, miembro, f"❌ Error al buscar el usuario: {e}"
    return usuario, miembro, None


def cargar_linkban():
    global linkban_canal
    if os.path.exists(LINKS_BANEADOS_PATH):
        try:
            with open(LINKS_BANEADOS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            linkban_canal = {int(x) for x in data.get("canales", [])}
        except (json.JSONDecodeError, OSError):
            linkban_canal = set()
    else:
        linkban_canal = set()
    print(f"Canales con links baneados cargados: {len(linkban_canal)}")


def guardar_linkban():
    try:
        with open(LINKS_BANEADOS_PATH, "w", encoding="utf-8") as f:
            json.dump({"canales": list(linkban_canal)}, f, indent=2)
    except OSError as e:
        print(f"Error guardando linkban_canal.json: {e}")


def cargar_warns():
    global warns_db
    if os.path.exists(WARNS_PATH):
        try:
            with open(WARNS_PATH, "r", encoding="utf-8") as f:
                warns_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            warns_db = {}
    else:
        warns_db = {}
    print(f"Warns cargados: {sum(len(v) for v in warns_db.values())} sobre {len(warns_db)} usuarios.")


def guardar_warns():
    try:
        with open(WARNS_PATH, "w", encoding="utf-8") as f:
            json.dump(warns_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando warns.json: {e}")


def cargar_logs_channels():
    global logs_channels
    if os.path.exists(LOGS_CHANNELS_PATH):
        try:
            with open(LOGS_CHANNELS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            logs_channels = {int(x) for x in data.get("canales", [])}
        except (json.JSONDecodeError, OSError):
            logs_channels = set()
    else:
        logs_channels = set()
    print(f"Canales de logs cargados: {len(logs_channels)}")


def guardar_logs_channels():
    try:
        with open(LOGS_CHANNELS_PATH, "w", encoding="utf-8") as f:
            json.dump({"canales": list(logs_channels)}, f, indent=2)
    except OSError as e:
        print(f"Error guardando logs_channels.json: {e}")


def guardar_logs_channels():
    try:
        with open(LOGS_CHANNELS_PATH, "w", encoding="utf-8") as f:
            json.dump({"canales": list(logs_channels)}, f, indent=2)
    except OSError as e:
        print(f"Error guardando logs_channels.json: {e}")


def cargar_honeypots():
    global honeypots_db
    if os.path.exists(HONEYPOTS_PATH):
        try:
            with open(HONEYPOTS_PATH, "r", encoding="utf-8") as f:
                honeypots_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            honeypots_db = {}
    else:
        honeypots_db = {}
    print(f"Honeypots cargados: {sum(len(v) for v in honeypots_db.values())} en {len(honeypots_db)} servidores.")


def guardar_honeypots():
    try:
        with open(HONEYPOTS_PATH, "w", encoding="utf-8") as f:
            json.dump(honeypots_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando honeypots.json: {e}")


def cargar_giveaways():
    global giveaways_db
    if os.path.exists(GIVEAWAYS_PATH):
        try:
            with open(GIVEAWAYS_PATH, "r", encoding="utf-8") as f:
                giveaways_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            giveaways_db = {}
    else:
        giveaways_db = {}
    print(f"Giveaways cargados: {len(giveaways_db)}")


def guardar_giveaways():
    try:
        with open(GIVEAWAYS_PATH, "w", encoding="utf-8") as f:
            json.dump(giveaways_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando giveaways.json: {e}")


def cargar_prefixes():
    global prefixes_db
    if os.path.exists(PREFIXES_PATH):
        try:
            with open(PREFIXES_PATH, "r", encoding="utf-8") as f:
                prefixes_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            prefixes_db = {}
    else:
        prefixes_db = {}
    print(f"Prefijos cargados para {len(prefixes_db)} servidores.")


def guardar_prefixes():
    try:
        with open(PREFIXES_PATH, "w", encoding="utf-8") as f:
            json.dump(prefixes_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando prefixes.json: {e}")


def cargar_reminders():
    global reminders_db
    if os.path.exists(REMINDERS_PATH):
        try:
            with open(REMINDERS_PATH, "r", encoding="utf-8") as f:
                reminders_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            reminders_db = {}
    else:
        reminders_db = {}
    print(f"Reminders cargados: {len(reminders_db)}")


def guardar_reminders():
    try:
        with open(REMINDERS_PATH, "w", encoding="utf-8") as f:
            json.dump(reminders_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando reminders.json: {e}")


async def enviar_logs(guild: discord.Guild, embed: discord.Embed):
    """Envía un embed de logs a todos los canales configurados en el servidor."""
    for canal_id in list(logs_channels):
        canal = guild.get_channel(canal_id)
        if canal is None:
            continue
        try:
            await canal.send(embed=embed)
        except discord.Forbidden:
            print(f"Sin permisos para enviar logs al canal {canal_id}")
        except discord.HTTPException as e:
            print(f"Error enviando logs a {canal_id}: {e}")


URL_REGEX = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+",
    re.IGNORECASE,
)


# Caché ligero de mensajes recientes para reconstruir el autor al borrar.
_cache_mensajes = {}


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    # Cachear para on_message_delete (sólo últimos 1000 para limitar RAM).
    if message.content or message.attachments:
        _cache_mensajes[message.id] = {
            "content": message.content or "",
            "author_id": message.author.id,
            "author_name": str(message.author),
            "author_avatar": message.author.display_avatar.url,
            "channel_id": message.channel.id,
            "guild_id": message.guild.id,
            "attachments": [a.url for a in message.attachments[:3]],
        }
        if len(_cache_mensajes) > 1000:
            # Eliminar las 200 entradas más viejas.
            _cache_mensajes.pop(next(iter(_cache_mensajes)), None)

    if message.channel.id in linkban_canal:
        if URL_REGEX.search(message.content or "") or message.attachments:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                await message.channel.send(
                    f"🚫 {message.author.mention}, no se permiten enlaces ni archivos en este canal.",
                    delete_after=5,
                )
            except discord.HTTPException:
                pass
            return

    # Honeypot check
    guild_id_str = str(message.guild.id)
    channel_id_str = str(message.channel.id)
    hp_config = honeypots_db.get(guild_id_str, {}).get(channel_id_str)
    if hp_config:
        # Delete the triggering message
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        # Apply sanction
        member = message.guild.get_member(message.author.id)
        if member:
            action = hp_config.get("action", "ban")
            duration = hp_config.get("duration")
            try:
                if action == "ban":
                    await message.guild.ban(message.author, reason="Honeypot trigger", delete_message_days=1)
                elif action == "kick":
                    await member.kick(reason="Honeypot trigger")
                elif action == "mute":
                    if duration:
                        until = discord.utils.utcnow() + datetime.timedelta(seconds=duration)
                        await member.timeout(until, reason="Honeypot trigger")
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass
        return

    await bot.process_commands(message)


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    cached = _cache_mensajes.pop(message.id, None)

    if cached is None:
        # No tenemos info guardada: usamos los datos que llegan en el payload.
        autor = message.author
        contenido = message.content or ""
        adjuntos = [a.url for a in message.attachments[:3]]
        autor_id = autor.id if autor else None
        autor_name = str(autor) if autor else "Desconocido"
        autor_avatar = autor.display_avatar.url if autor else None
    else:
        contenido = cached["content"]
        autor_id = cached["author_id"]
        autor_name = cached["author_name"]
        autor_avatar = cached["author_avatar"]
        adjuntos = cached["attachments"]

    # Determinar quién borró el mensaje vía audit log (el más reciente en MESSAGE_DELETE).
    autor_borrado = None
    try:
        async for entry in message.guild.audit_logs(
            action=discord.AuditLogAction.message_delete, limit=5
        ):
            if entry.target and entry.target.id == autor_id and entry.channel and entry.channel.id == message.channel.id:
                # Decisión: apenas pasa unos segundos desde el borrado.
                if (datetime.datetime.now(datetime.timezone.utc) - entry.created_at).total_seconds() < 15:
                    autor_borrado = entry.user
                    break
    except discord.Forbidden:
        pass
    except Exception:
        pass

    contenido_mostrar = contenido[:1024] if contenido else "*(sin texto)*"
    embed = discord.Embed(
        title="🗑️ Mensaje borrado",
        color=discord.Color.dark_red(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="Canal",
        value=f"{message.channel.mention} (`{message.channel.id}`)",
        inline=False,
    )
    embed.add_field(
        name="Autor del mensaje",
        value=f"{autor_name} (`{autor_id}`)" if autor_id else "Desconocido",
        inline=True,
    )
    embed.add_field(
        name="Borrado por",
        value=f"{autor_borrado.mention} (`{autor_borrado.id}`)" if autor_borrado else "Desconocido (¿ Discord o autor ?)",
        inline=True,
    )
    embed.add_field(name="Contenido", value=contenido_mostrar, inline=False)
    if adjuntos:
        embed.add_field(name="Adjuntos", value="\n".join(adjuntos)[:1024], inline=False)
    if autor_avatar:
        embed.set_thumbnail(url=autor_avatar)
    embed.set_footer(text=f"ID del mensaje: {message.id}")

    await enviar_logs(message.guild, embed)


@bot.event
async def on_ready():
    cargar_linkban()
    cargar_warns()
    cargar_logs_channels()
    cargar_giveaways()
    cargar_prefixes()
    cargar_reminders()
    cargar_honeypots()
    print(f"Conectado como {bot.user} (ID: {bot.user.id})")
    print(f"Servidores: {len(bot.guilds)}")
    # Intentar sync global primero.
    try:
        synced = await bot.tree.sync()
        print(f"Comandos slash sincronizados (global): {len(synced)}")
        # Si hay guilds, hacer sync por guild (instantáneo en esos servidores).
        for guild in bot.guilds:
            try:
                synced_g = await bot.tree.sync(guild=guild)
                print(f"  Sincronizados en {guild.name} (ID {guild.id}): {len(synced_g)}")
            except Exception as e:
                print(f"  Error sincronizando guild {guild.id}: {e}")
    except Exception as e:
        print(f"Error al sincronizar slash commands (global): {e}")
    # Reanudar giveaways y reminders activos.
    bot.loop.create_task(_reanudar_giveaways())
    bot.loop.create_task(_reanudar_reminders())


@bot.command(name="sync")
@commands.is_owner()
async def sync_manual(ctx):
    """Vuelve a sincronizar los slash commands (solo el dueño del bot)."""
    try:
        n_global = len(await bot.tree.sync())
        for guild in bot.guilds:
            await bot.tree.sync(guild=guild)
        await ctx.send(f"✅ Sincronizados {n_global} slash commands globales + en {len(bot.guilds)} servidores.")
    except Exception as e:
        await ctx.send(f"❌ Error al sincronizar: {e}")


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx, usuario_arg: str, *, motivo: str = "No especificado"):
    """Banea a un usuario por ID, @mención o nombre de usuario. Uso: .ban <id|@|nombre> [motivo]"""
    usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)

    if miembro is not None:
        if miembro.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ No puedes banear a alguien con un rol igual o superior al tuyo.")
        if ctx.guild.me.top_role <= miembro.top_role:
            return await ctx.send("❌ Mi rol es inferior al de ese usuario, no puedo banearlo.")

    try:
        await ctx.guild.ban(usuario, reason=f"{ctx.author} (ID {ctx.author.id}): {motivo}", delete_message_days=1)
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para banear a ese usuario.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al banear: {e}")

    embed = discord.Embed(
        title="🔨 Usuario baneado",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Usuario", value=f"{usuario} (`{usuario.id}`)", inline=False)
    embed.add_field(name="Moderador", value=f"{ctx.author.mention}", inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=True)
    embed.set_thumbnail(url=usuario.display_avatar.url)
    await ctx.send(embed=embed)


@ban.error
async def ban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ No tienes permiso para banear miembros.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.ban <id_usuario> [motivo]`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me faltan permisos (Ban members) para ejecutar este comando.")


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx, usuario_arg: str, *, motivo: str = "No especificado"):
    """Expulsa a un miembro por ID, @mención o nombre. Uso: .kick <id|@|nombre> [motivo]"""
    usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)
    if miembro is None:
        return await ctx.send("❌ Ese usuario no está en este servidor, no puedo expulsarlo.")

    if miembro.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send("❌ No puedes expulsar a alguien con un rol igual o superior al tuyo.")
    if ctx.guild.me.top_role <= miembro.top_role:
        return await ctx.send("❌ Mi rol es inferior al de ese usuario, no puedo expulsarlo.")

    try:
        await miembro.kick(reason=f"{ctx.author} (ID {ctx.author.id}): {motivo}")
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para expulsar a ese usuario.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al expulsar: {e}")

    embed = discord.Embed(
        title="👢 Usuario expulsado",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Usuario", value=f"{miembro} (`{miembro.id}`)", inline=False)
    embed.add_field(name="Moderador", value=f"{ctx.author.mention}", inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=True)
    embed.set_thumbnail(url=usuario.display_avatar.url)
    await ctx.send(embed=embed)


@kick.error
async def kick_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ No tienes permiso para expulsar miembros.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.kick <id_usuario> [motivo]`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me faltan permisos (Kick members) para ejecutar este comando.")


@bot.command(name="mute")
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def mute(ctx, usuario_arg: str, duracion: str, *, motivo: str = "No especificado"):
    """
    Silencia (timeout) a un miembro por ID, @mención o nombre.
    Uso: .mute <id|@|nombre> <duración> [motivo]
    La duración usa h/m/s. Ejemplos: 5h, 30m, 10s, 1h30m, 2h15m30s.
    """
    segundos, err = parsear_duracion(duracion)
    if err:
        return await ctx.send(f"❌ {err}")

    usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)
    if miembro is None:
        return await ctx.send(f"❌ Ese usuario no está en este servidor, no puedo silenciarlo.")

    if miembro.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send("❌ No puedes silenciar a alguien con un rol igual o superior al tuyo.")
    if ctx.guild.me.top_role <= miembro.top_role:
        return await ctx.send("❌ Mi rol es inferior al de ese usuario, no puedo silenciarlo.")
    if ctx.guild.owner_id == miembro.id:
        return await ctx.send("❌ No puedo silenciar al dueño del servidor.")

    try:
        hasta = discord.utils.utcnow() + datetime.timedelta(seconds=segundos)
        await miembro.timeout(hasta, reason=f"{ctx.author} (ID {ctx.author.id}): {motivo}")
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para silenciar a ese usuario.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al silenciar: {e}")

    # Tarea en segundo plano para quitar el timeout cuando termine.
    bot.loop.create_task(_quitar_timeout_automatico(ctx.guild.id, miembro.id, segundos, ctx.author))

    humanas = []
    h = segundos // 3600
    m = (segundos % 3600) // 60
    s = segundos % 60
    if h: humanas.append(f"{h}h")
    if m: humanas.append(f"{m}m")
    if s: humanas.append(f"{s}s")
    duracion_str = " ".join(humanas) or "0s"

    embed = discord.Embed(
        title="🔇 Usuario silenciado",
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Usuario", value=f"{miembro.mention} (`{miembro.id}`)", inline=False)
    embed.add_field(name="Moderador", value=f"{ctx.author.mention}", inline=True)
    embed.add_field(name="Duración", value=duracion_str, inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.set_thumbnail(url=miembro.display_avatar.url)
    embed.set_footer(text=f"Se quitará automáticamente a las {discord.utils.format_dt(hasta, 'T')}")
    await ctx.send(embed=embed)


async def _quitar_timeout_automatico(guild_id, user_id, segundos, moderador):
    await asyncio.sleep(segundos)
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    miembro = guild.get_member(user_id)
    if miembro is None:
        return
    if miembro.is_timed_out():
        try:
            await miembro.timeout(None, reason="Timeout automático finalizado.")
            print(f"Timeout automático finalizado para {miembro} ({miembro.id}).")
        except discord.HTTPException:
            pass


@mute.error
async def mute_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ No tienes permiso para silenciar miembros (Timeout members).")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.mute <id_usuario> <duración> [motivo]`\nEj: `.mute 123456789 5h Spam`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me faltan permisos (Timeout members) para ejecutar este comando.")


@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def unmute(ctx, usuario_arg: str, *, motivo: str = "No especificado"):
    """Quita el timeout a un miembro. Uso: .unmute <id|@|nombre> [motivo]"""
    usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)
    if miembro is None:
        return await ctx.send("❌ Ese usuario no está en este servidor.")
    if not miembro.is_timed_out():
        return await ctx.send("ℹ️ Ese usuario no está silenciado.")
    try:
        await miembro.timeout(None, reason=f"{ctx.author} (ID {ctx.author.id}): {motivo}")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al quitar el silencio: {e}")
    await ctx.send(f"✅ {miembro.mention} ya puede hablar de nuevo.")


@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx, usuario_arg: str, *, motivo: str = "No especificado"):
    """Levanta el ban de un usuario por ID, @mención o nombre. Uso: .unban <id|@|nombre> [motivo]"""
    # Nota: unban por nombre requiere que la caché del bot tenga el usuario; lo más fiable es ID/@.
    usuario, _, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        # Si vino un nombre y no está en caché, intentamos buscar en la lista de baneados.
        m = None
        try:
            bans = await ctx.guild.bans()
            for entry in bans:
                if entry.user.name.lower() == usuario_arg.lower():
                    usuario = entry.user
                    err = None
                    break
        except discord.HTTPException:
            pass
        if err:
            return await ctx.send(err)
    try:
        await ctx.guild.unban(usuario, reason=f"{ctx.author} (ID {ctx.author.id}): {motivo}")
    except discord.NotFound:
        return await ctx.send("ℹ️ Ese usuario no estaba baneado.")
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para desbanear.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al desbanear: {e}")
    await ctx.send(f"✅ {usuario} (`{usuario.id}`) ha sido desbaneado.")


@bot.command(name="warn")
@commands.has_permissions(moderate_members=True)
async def warn(ctx, usuario_arg: str, *, motivo: str):
    """Advierte a un usuario. Uso: .warn <id|@|nombre> <motivo> (motivo obligatorio)"""
    if not motivo or motivo.strip() == "":
        return await ctx.send("❌ Debes indicar un motivo. Uso: `.warn <id|@|nombre> <motivo>`")

    usuario, _, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)

    clave = str(usuario.id)
    lista = warns_db.setdefault(clave, [])

    numero = len(lista) + 1
    entrada = {
        "numero": numero,
        "motivo": motivo.strip(),
        "moderador_id": ctx.author.id,
        "moderador": str(ctx.author),
        "fecha": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    lista.append(entrada)
    guardar_warns()

    embed = discord.Embed(
        title="⚠️ Advertencia aplicada",
        color=discord.Color.gold(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="Usuario", value=f"{usuario} (`{usuario.id}`)", inline=False)
    embed.add_field(name="Moderador", value=ctx.author.mention, inline=True)
    embed.add_field(name="Número de warn", value=f"**#{numero}**", inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.set_thumbnail(url=usuario.display_avatar.url)
    embed.set_footer(text=f"Total de warns: {len(lista)}")
    await ctx.send(embed=embed)


@warn.error
async def warn_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Timeout Members para advertir.")
    elif isinstance(error, commands.MissingRequiredArgument):
        if "motivo" in str(error):
            await ctx.send("❌ Debes indicar un motivo. Uso: `.warn <id> <motivo>`")
        else:
            await ctx.send("❌ Uso correcto: `.warn <id_usuario> <motivo>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")


@bot.command(name="warnremove")
@commands.has_permissions(moderate_members=True)
async def warnremove(ctx, usuario_arg: str, numero: int):
    """Elimina un warn por su número. Uso: .warnremove <id|@|nombre> <número>"""
    usuario, _, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)
    clave = str(usuario.id)
    lista = warns_db.get(clave, [])
    if not lista:
        return await ctx.send(f"ℹ️ {usuario} no tiene ningún warn.")

    warn_a_borrar = None
    for w in lista:
        if w["numero"] == numero:
            warn_a_borrar = w
            break

    if warn_a_borrar is None:
        return await ctx.send(f"❌ No existe el warn #{numero}. El usuario tiene warns del 1 al {len(lista)}.")

    lista.remove(warn_a_borrar)
    # Renumerar los restantes (1..N) para mantener consistencia.
    for i, w in enumerate(lista, start=1):
        w["numero"] = i
    guardar_warns()

    embed = discord.Embed(
        title="✅ Warn eliminado",
        color=discord.Color.green(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="Usuario", value=f"<@{usuario.id}> (`{usuario.id}`)", inline=False)
    embed.add_field(name="Warn eliminado", value=f"#{numero}", inline=True)
    embed.add_field(name="Motivo original", value=warn_a_borrar["motivo"][:1024], inline=False)
    embed.set_footer(text=f"Warns restantes: {len(lista)}")
    await ctx.send(embed=embed)


@warnremove.error
async def warnremove_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Timeout Members.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.warnremove <id_usuario> <número>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID y el número deben ser enteros.")


@bot.command(name="warns")
@commands.has_permissions(moderate_members=True)
async def warns(ctx, usuario_arg: str):
    """Muestra todos los warns de un usuario con motivo y fecha. Uso: .warns <id|@|nombre>"""
    usuario, _, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)
    user_id = usuario.id
    clave = str(user_id)
    lista = warns_db.get(clave, [])
    nombre_usuario = f"{usuario}"

    if not lista:
        embed = discord.Embed(
            title="✅ Sin advertencias",
            description=f"{nombre_usuario} (`{user_id}`) no tiene ningún warn.",
            color=discord.Color.green(),
        )
        return await ctx.send(embed=embed)

    embed = discord.Embed(
        title=f"⚠️ Warns de {nombre_usuario}",
        color=discord.Color.gold(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_footer(text=f"Total: {len(lista)} advertencia(s)")

    for w in lista:
        try:
            fecha_dt = datetime.datetime.fromisoformat(w["fecha"])
            fecha_str = discord.utils.format_dt(fecha_dt, "f")
        except (ValueError, KeyError):
            fecha_str = "Fecha desconocida"
        embed.add_field(
            name=f"#{w['numero']} — {fecha_str}",
            value=(f"**Motivo:** {w['motivo'][:900]}\n"
                   f"**Moderador:** {w['moderador']}"),
            inline=False,
        )
    await ctx.send(embed=embed)


@warns.error
async def warns_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Timeout Members.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.warns <id_usuario>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")


@bot.command(name="logchannel")
@commands.has_permissions(manage_guild=True)
async def logchannel(ctx, canal: discord.TextChannel):
    """Asigna un canal para enviar logs. Uso: .logchannel #canal"""
    if canal.id in logs_channels:
        return await ctx.send(f"ℹ️ {canal.mention} ya estaba configurado como canal de logs.")
    logs_channels.add(canal.id)
    guardar_logs_channels()
    await ctx.send(f"✅ {canal.mention} configurado como canal de logs.")


@logchannel.error
async def logchannel_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Server.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.logchannel #canal`")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("❌ No encontré ese canal. Uso: `.logchannel #canal`")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me faltan permisos.")


@bot.command(name="logunchannel")
@commands.has_permissions(manage_guild=True)
async def logunchannel(ctx, canal: discord.TextChannel):
    """Quita un canal de la lista de logs. Uso: .logunchannel #canal"""
    if canal.id not in logs_channels:
        return await ctx.send(f"ℹ️ {canal.mention} no estaba configurado como canal de logs.")
    logs_channels.discard(canal.id)
    guardar_logs_channels()
    await ctx.send(f"✅ {canal.mention} ya no recibirá logs.")


@logunchannel.error
async def logunchannel_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Server.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.logunchannel #canal`")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("❌ No encontré ese canal.")


@bot.command(name="logschannels")
@commands.has_permissions(manage_guild=True)
async def logschannels(ctx):
    """Lista todos los canales configurados para logs."""
    if not logs_channels:
        return await ctx.send("ℹ️ No hay canales de logs configurados. Usa `.logchannel #canal` para añadir uno.")

    embed = discord.Embed(
        title="📋 Canales configurados para logs",
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    existe_alguno = False
    lineas = []
    for canal_id in sorted(logs_channels):
        canal = ctx.guild.get_channel(canal_id)
        if canal is None:
            lineas.append(f"• `<#{canal_id}>` *(canal ya no existe)*")
        else:
            existe_alguno = True
            lineas.append(f"• {canal.mention} (`{canal.id}`)")
    embed.description = "\n".join(lineas)
    embed.set_footer(text=f"Total: {len(logs_channels)} canal(es)")
    await ctx.send(embed=embed)


@logschannels.error
async def logschannels_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Server.")


@bot.command(name="linkban")
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True)
async def linkban(ctx, canal: discord.TextChannel):
    """Prohibe enlaces y archivos en un canal. Uso: .linkban #canal"""
    if canal.id in linkban_canal:
        return await ctx.send(f"ℹ️ {canal.mention} ya tenía los enlaces prohibidos.")
    linkban_canal.add(canal.id)
    guardar_linkban()
    await ctx.send(f"✅ Enlaces y archivos prohibidos en {canal.mention}.")


@linkban.error
async def linkban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Messages.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.linkban #canal`")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send(f"❌ No encontré ese canal. Uso: `.linkban #canal`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Debes mencionar un canal válido. Uso: `.linkban #canal`")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Messages.")


@bot.command(name="linkunban")
@commands.has_permissions(manage_messages=True)
async def linkunban(ctx, canal: discord.TextChannel):
    """Permite de nuevo enlaces en un canal. Uso: .linkunban #canal"""
    if canal.id not in linkban_canal:
        return await ctx.send(f"ℹ️ {canal.mention} no tenía los enlaces prohibidos.")
    linkban_canal.discard(canal.id)
    guardar_linkban()
    await ctx.send(f"✅ Enlaces permitidos de nuevo en {canal.mention}.")


@linkunban.error
async def linkunban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Messages.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.linkunban #canal`")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send(f"❌ No encontré ese canal. Uso: `.linkunban #canal`")


@bot.command(name="linkbanlist")
@commands.has_permissions(manage_messages=True)
async def linkbanlist(ctx):
    """Lista los canales con enlaces prohibidos."""
    if not linkban_canal:
        return await ctx.send("ℹ️ No hay canales con enlaces prohibidos.")
    canales = [f"<#{cid}>" for cid in sorted(linkban_canal)]
    embed = discord.Embed(
        title="🔗 Canales con enlaces prohibidos",
        description="\n".join(canales),
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


@bot.command(name="roleadd")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def roleadd(ctx, usuario_arg: str, rol: discord.Role):
    """Otorga un rol a un usuario. Uso: .roleadd <id|@|nombre> @rol"""
    usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)
    if miembro is None:
        return await ctx.send("❌ Ese usuario no está en este servidor.")

    if rol.position >= ctx.author.top_role.position and ctx.author != ctx.guild.owner:
        return await ctx.send("❌ No puedes otorgar un rol igual o superior al tuyo.")
    if rol.position >= ctx.guild.me.top_role.position:
        return await ctx.send("❌ Ese rol está por encima del mío, no puedo asignarlo.")
    if rol.managed:
        return await ctx.send("❌ Ese rol está gestionado por una integración/bot, no puedo asignarlo.")

    if rol in miembro.roles:
        return await ctx.send(f"ℹ️ {miembro.mention} ya tiene el rol {rol.mention}.")

    try:
        await miembro.add_roles(rol, reason=f"{ctx.author} (ID {ctx.author.id})")
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para asignar ese rol.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al asignar el rol: {e}")

    await ctx.send(f"✅ Rol {rol.mention} asignado a {miembro.mention}.")


@roleadd.error
async def roleadd_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Roles.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.roleadd <id_usuario> @rol`")
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send("❌ No encontré ese rol. Menciónalo con @rol.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Roles.")


@bot.command(name="roleremove")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def roleremove(ctx, usuario_arg: str, rol: discord.Role):
    """Quita un rol a un usuario. Uso: .roleremove <id|@|nombre> @rol"""
    usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)
    if miembro is None:
        return await ctx.send("❌ Ese usuario no está en este servidor.")

    if rol.position >= ctx.author.top_role.position and ctx.author != ctx.guild.owner:
        return await ctx.send("❌ No puedes quitar un rol igual o superior al tuyo.")
    if rol.position >= ctx.guild.me.top_role.position:
        return await ctx.send("❌ Ese rol está por encima del mío, no puedo quitarlo.")
    if rol.managed:
        return await ctx.send("❌ Ese rol está gestionado por una integración/bot.")

    if rol not in miembro.roles:
        return await ctx.send(f"ℹ️ {miembro.mention} no tiene el rol {rol.mention}.")

    try:
        await miembro.remove_roles(rol, reason=f"{ctx.author} (ID {ctx.author.id})")
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para quitar ese rol.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al quitar el rol: {e}")

    await ctx.send(f"✅ Rol {rol.mention} quitado a {miembro.mention}.")


@bot.command(name="avatar")
async def avatar(ctx, usuario_arg: str):
    """Muestra el avatar completo de un usuario. Uso: .avatar <id|@|nombre>"""
    usuario, _, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)

    avatar_url = usuario.display_avatar.with_size(4096).url
    embed = discord.Embed(
        title=f"Avatar de {usuario}",
        color=discord.Color.blurple(),
    )
    embed.set_image(url=avatar_url)
    embed.set_footer(text=f"ID: {usuario.id}")
    await ctx.send(embed=embed)


@avatar.error
async def avatar_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.avatar <id|@|nombre>`")


@bot.command(name="banner")
async def banner(ctx, usuario_arg: str):
    """Muestra el banner completo de un usuario. Uso: .banner <id|@|nombre>"""
    usuario, _, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)

    banner_obj = usuario.banner
    if banner_obj is None:
        return await ctx.send(f"ℹ️ {usuario} no tiene banner de perfil.")

    color_hex = usuario.accent_color
    if color_hex is not None:
        color_embed = color_hex
        descripcion = f"**Color de acento:** `#{color_hex.value:06X}`"
    else:
        color_embed = discord.Color.blurple()
        descripcion = "Sin color de acento."

    banner_url = banner_obj.with_size(4096).url
    embed = discord.Embed(
        title=f"Banner de {usuario}",
        description=descripcion,
        color=color_embed,
    )
    embed.set_image(url=banner_url)
    embed.set_footer(text=f"ID: {usuario.id}")
    await ctx.send(embed=embed)


@banner.error
async def banner_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.banner <id|@|nombre>`")


# ============================================================
#  COMANDOS MASIVOS DE ROLES (humanos / bots / todos)
# ============================================================

async def _aplicar_role_masivo(ctx, rol: discord.Role, filtro: str):
    """
    filtro: 'humanos' | 'bots' | 'todos'
    Aplica el rol en lotes con barra de progreso por mensaje editado. Evita rate limits.
    """
    if rol.position >= ctx.author.top_role.position and ctx.author != ctx.guild.owner:
        return await ctx.send("❌ No puedes otorgar un rol igual o superior al tuyo.")
    if rol.position >= ctx.guild.me.top_role.position:
        return await ctx.send("❌ Ese rol está por encima del mío, no puedo asignarlo.")
    if rol.managed:
        return await ctx.send("❌ Ese rol está gestionado por una integración y no puedo asignarlo.")

    miembros = ctx.guild.members
    if filtro == "humanos":
        objetivos = [m for m in miembros if not m.bot and rol not in m.roles]
        descripcion = "miembros humanos"
    elif filtro == "bots":
        objetivos = [m for m in miembros if m.bot and rol not in m.roles]
        descripcion = "bots"
    else:  # todos
        objetivos = [m for m in miembros if rol not in m.roles]
        descripcion = "miembros"

    total = len(objetivos)
    if total == 0:
        return await ctx.send(f"ℹ️ No hay {descripcion} sin el rol {rol.mention}.")

    msg = await ctx.send(f"⏳ Asignando {rol.mention} a **{total}** {descripcion}... (`0/{total}`)")
    asignados = 0
    errores = 0
    for i, miembro in enumerate(objetivos, start=1):
        try:
            await miembro.add_roles(rol, reason=f"{ctx.author} (ID {ctx.author.id}) — asignación masiva ({filtro})")
            asignados += 1
        except discord.Forbidden:
            errores += 1
        except discord.HTTPException:
            errores += 1
        # Editar cada 10 para no spamear la API.
        if i % 10 == 0 or i == total:
            try:
                await msg.edit(content=f"⏳ Asignando {rol.mention} a {descripcion}... (`{i}/{total}`)")
            except discord.HTTPException:
                pass

    embed = discord.Embed(
        title=f"✅ Asignación masiva completada",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Rol", value=rol.mention, inline=True)
    embed.add_field(name="Filtro", value=filtro.capitalize(), inline=True)
    embed.add_field(name="Asignados", value=f"**{asignados}/{total}**", inline=True)
    if errores:
        embed.add_field(name="Errores", value=str(errores), inline=True)
    embed.set_footer(text=f"Ejecutado por {ctx.author}")
    await ctx.send(embed=embed)


@bot.command(name="rolehuman")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def rolehuman(ctx, rol: discord.Role):
    """Asigna un rol a todos los miembros HUMANOS del servidor. Uso: .rolehuman @rol"""
    await _aplicar_role_masivo(ctx, rol, "humanos")


@rolehuman.error
async def rolehuman_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Roles.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.rolehuman @rol`")
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send("❌ No encontré ese rol. Menciónalo con @rol.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Roles.")


@bot.command(name="roleall")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def roleall(ctx, rol: discord.Role):
    """Asigna un rol a todos los miembros (humanos y bots). Uso: .roleall @rol"""
    await _aplicar_role_masivo(ctx, rol, "todos")


@roleall.error
async def roleall_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Roles.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.roleall @rol`")
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send("❌ No encontré ese rol. Menciónalo con @rol.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Roles.")


@bot.command(name="rolebot")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def rolebot(ctx, rol: discord.Role):
    """Asigna un rol solo a los BOTS del servidor. Uso: .rolebot @rol"""
    await _aplicar_role_masivo(ctx, rol, "bots")


@rolebot.error
async def rolebot_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Roles.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.rolebot @rol`")
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send("❌ No encontré ese rol. Menciónalo con @rol.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Roles.")


# ============================================================
#  PURGE y NUKE
# ============================================================

@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def purge(ctx, cantidad: int):
    """Elimina una cantidad de mensajes del canal actual y reacciona con ✅ al mensaje del comando."""
    if cantidad < 1:
        return await ctx.send("❌ La cantidad debe ser mayor que 0.")
    if cantidad > 1000:
        return await ctx.send("❌ El máximo permitido por Discord es 1000 mensajes por comando.")

    # Borrar solo los mensajes indicados (sin contar el del comando, que se conserva para reaccionar).
    eliminados = 0
    restante = cantidad
    while restante > 0:
        batch = min(restante, 100)
        try:
            borrados = await ctx.channel.purge(limit=batch, check=lambda m: m.id != ctx.message.id)
        except discord.Forbidden:
            return await ctx.send("❌ No tengo permisos para borrar mensajes.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Error al borrar: {e}")
        eliminados += len(borrados)
        restante -= batch
        if len(borrados) < batch:
            break  # No hay más mensajes.

    # Conservar el mensaje del comando y solo reaccionar con ✅ (sin texto del bot).
    try:
        await ctx.message.add_reaction("✅")
    except discord.HTTPException:
        pass


@purge.error
async def purge_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Messages.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.purge <cantidad>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La cantidad debe ser un número entero.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me faltan permisos (Manage Messages + Read Message History).")


class ConfirmNukeView(discord.ui.View):
    """Vista con botones Confirmar/Cancelar para el comando nuke."""
    def __init__(self, autor_id: int, canal: discord.TextChannel, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.autor_id = autor_id
        self.canal = canal
        self.confirmado = False

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success, emoji="✅")
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.autor_id:
            return await interaction.response.send_message("❌ Solo quien lanzó el comando puede confirmar.", ephemeral=True)
        self.confirmado = True
        self.stop()
        await interaction.response.edit_message(content="💥 Nuke confirmado, ejecutando...", view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.autor_id:
            return await interaction.response.send_message("❌ Solo quien lanzó el comando puede cancelar.", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(content="❌ Nuke cancelado.", view=None)

    async def on_timeout(self):
        try:
            if not self.confirmado and self.message:
                await self.message.edit(content="⏱️ Nuke cancelado por inactividad.", view=None)
        except discord.HTTPException:
            pass


@bot.command(name="nuke")
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, manage_channels=True)
async def nuke(ctx, canal: discord.TextChannel = None):
    """
    Elimina TODOS los mensajes de un canal clonándolo (mantiene permisos y posición).
    Antes pide confirmación mediante botones.
    Si no se especifica canal, actúa sobre el canal actual.
    Uso: .nuke [#canal]
    """
    canal = canal or ctx.channel

    if canal.id != ctx.channel.id:
        if ctx.author != ctx.guild.owner and ctx.author.top_role.position <= canal.permissions_for(ctx.author).view_channel and not ctx.author.guild_permissions.administrator:
            return await ctx.send("❌ No tienes permiso sobre ese canal.")

    view = ConfirmNukeView(ctx.author.id, canal)
    msg = await ctx.send(f"⚠️ ¿Seguro que quieres eliminar **TODOS** los mensajes de {canal.mention}? Tienes 30 segundos para confirmar.", view=view)
    view.message = msg
    await view.wait()

    if not view.confirmado:
        return  # Cancelado o timeout; ya se informó en la vista.

    try:
        posicion = canal.position
        categoria = canal.category
        nuevo = await canal.clone(name=canal.name, reason=f"Nuke ejecutado por {ctx.author} (ID {ctx.author.id})")
        await nuevo.edit(position=posicion, category=categoria, topic=canal.topic, nsfw=canal.nsfw, slowmode_delay=canal.slowmode_delay)
        await canal.delete(reason=f"Nuke ejecutado por {ctx.author} (ID {ctx.author.id})")
    except discord.Forbidden:
        return await ctx.send(f"❌ No tengo permisos para clonar/borrar {canal.mention}. Necesito Manage Channels.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al nukear: {e}")

    try:
        await nuevo.send(f"💥 **Nuke ejecutado por {ctx.author.mention}** — canal restablecido.")
    except discord.HTTPException:
        pass


@nuke.error
async def nuke_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Messages.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me faltan permisos (Manage Messages + Manage Channels).")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("❌ No encontré ese canal. Uso: `.nuke [#canal]`")


# ============================================================
#  GIVEAWAYS
# ============================================================

def _embed_giveaway(gw: dict) -> discord.Embed:
    """Construye el embed estándar de un giveaway."""
    final = datetime.datetime.fromisoformat(gw["fin"])
    final_str = discord.utils.format_dt(final, "R")
    embed = discord.Embed(
        title=f"🎉 {gw['nombre']}",
        description=(f"Reacciona con 🎉 para participar.\n\n"
                     f"**Ganadores:** {gw['ganadores_n']}\n"
                     f"**Finaliza:** {final_str}\n"
                     f"**Host:** <@{gw['host_id']}>"),
        color=discord.Color.gold(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if gw.get("ganadores"):
        ganadores_str = " ".join(f"<@{g}>" for g in gw["ganadores"])
        embed.add_field(name="🏆 Ganador(es)", value=ganadores_str, inline=False)
        embed.color = discord.Color.green()
    estado = "✅ Finalizado" if gw.get("finalizado") else "⏳ En curso"
    embed.set_footer(text=f"Giveaway #{gw['numero']} • {estado}")
    return embed


async def _tarea_giveaway(gw_id: str):
    """Espera a la hora prevista, sortea y actualiza el mensaje."""
    gw = giveaways_db.get(gw_id)
    if gw is None or gw.get("finalizado"):
        return
    fin_dt = datetime.datetime.fromisoformat(gw["fin"])
    ahora = datetime.datetime.now(datetime.timezone.utc)
    segundos_restantes = (fin_dt - ahora).total_seconds()
    if segundos_restantes > 0:
        await asyncio.sleep(segundos_restantes)

    gw = giveaways_db.get(gw_id)
    if gw is None or gw.get("finalizado"):
        return

    try:
        guild = bot.get_guild(int(gw["guild_id"]))
        canal = guild.get_channel(int(gw["canal_id"])) if guild else None
        mensaje = await canal.fetch_message(int(gw["mensaje_id"])) if canal else None
    except (discord.HTTPException, AttributeError):
        mensaje = None

    # Contar reacciones 🎉.
    participantes = []
    if mensaje is not None:
        for reaction in mensaje.reactions:
            if str(reaction.emoji) == "🎉":
                participantes = [u async for u in reaction.users() if not u.bot and u.id != bot.user.id]
                break

    ganadores = []
    n = gw["ganadores_n"]
    if participantes:
        random.shuffle(participantes)
        ganadores = [p.id for p in participantes[:n]]

    gw["ganadores"] = ganadores
    gw["finalizado"] = True
    guardar_giveaways()

    embed = _embed_giveaway(gw)
    if mensaje is not None:
        try:
            await mensaje.edit(embed=embed)
            await mensaje.clear_reactions()
        except discord.HTTPException:
            pass

    if canal is not None:
        if ganadores:
            menciones = " ".join(f"<@{g}>" for g in ganadores)
            try:
                await canal.send(f"🎉 ¡Felicidades {menciones}! Has ganado **{gw['nombre']}**!\n(Giveaway #{gw['numero']})")
            except discord.HTTPException:
                pass
        else:
            try:
                await canal.send(f"⚠️ No había participantes válidos en el giveaway #{gw['numero']} **{gw['nombre']}**.")
            except discord.HTTPException:
                pass


async def _reanudar_giveaways():
    """Reanuda los giveaways activos tras reiniciar el bot."""
    for gw_id, gw in list(giveaways_db.items()):
        if gw.get("finalizado"):
            continue
        fin_dt = datetime.datetime.fromisoformat(gw["fin"])
        if datetime.datetime.now(datetime.timezone.utc) >= fin_dt:
            await _tarea_giveaway(gw_id)
        else:
            bot.loop.create_task(_tarea_giveaway(gw_id))


@bot.command(name="gcreate")
@commands.has_permissions(manage_messages=True)
async def gcreate(ctx, nombre: str, duracion: str, ganadores_n: int):
    """Crea un sorteo. Uso: .gcreate <nombre> <duración> <número de ganadores>"""
    if not nombre or len(nombre) > 100:
        return await ctx.send("❌ El nombre es obligatorio y como máximo 100 caracteres.")
    segundos, err = parsear_duracion(duracion)
    if err:
        return await ctx.send(f"❌ {err}")
    if ganadores_n < 1 or ganadores_n > 20:
        return await ctx.send("❌ Indica entre 1 y 20 ganadores.")
    if segundos > 86400:
        return await ctx.send("❌ La duración máxima de un giveaway es de 24 horas.")

    # Numeración final global.
    gw_id = f"{ctx.guild.id}-{len(giveaways_db) + 1}-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}"
    fin_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=segundos)
    gw = {
        "numero": len([g for g in giveaways_db.values() if g["guild_id"] == str(ctx.guild.id)]) + 1,
        "guild_id": str(ctx.guild.id),
        "canal_id": str(ctx.channel.id),
        "mensaje_id": None,
        "nombre": nombre,
        "fin": fin_dt.isoformat(),
        "ganadores_n": ganadores_n,
        "ganadores": [],
        "finalizado": False,
        "host_id": str(ctx.author.id),
    }

    embed = _embed_giveaway(gw)
    mensaje = await ctx.send(embed=embed)
    try:
        await mensaje.add_reaction("🎉")
    except discord.HTTPException:
        pass

    gw["mensaje_id"] = str(mensaje.id)
    giveaways_db[gw_id] = gw
    guardar_giveaways()

    bot.loop.create_task(_tarea_giveaway(gw_id))
    await ctx.send(f"✅ Sorteo **{nombre}** creado (ID `#{gw['numero']}`). Finaliza en `{duracion}`.", delete_after=10)


@gcreate.error
async def gcreate_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Messages.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.gcreate <nombre> <duración> <número_ganadores>`\nEj: `.gcreate \"Nitro Classic\" 24h 2`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ El número de ganadores debe ser un entero.")


def _buscar_giveaway_por_numero(guild_id: int, numero: int):
    for gw_id, gw in giveaways_db.items():
        if gw["guild_id"] == str(guild_id) and gw["numero"] == numero:
            return gw_id, gw
    return None, None


@bot.command(name="glist")
@commands.has_permissions(manage_messages=True)
async def glist(ctx):
    """Muestra todos los sorteos (activos y finalizados) del servidor."""
    lista = [(gw_id, gw) for gw_id, gw in giveaways_db.items() if gw["guild_id"] == str(ctx.guild.id)]
    if not lista:
        return await ctx.send("ℹ️ No hay sorteos en este servidor.")

    embed = discord.Embed(title="🎉 Sorteos del servidor", color=discord.Color.blurple(), timestamp=datetime.datetime.now(datetime.timezone.utc))
    embed.set_footer(text=f"Total: {len(lista)}")
    for gw_id, gw in lista[:25]:
        estado = "✅ Finalizado" if gw.get("finalizado") else "⏳ En curso"
        fin_dt = datetime.datetime.fromisoformat(gw["fin"])
        fin_str = discord.utils.format_dt(fin_dt, "R")
        embed.add_field(
            name=f"#{gw['numero']} — {gw['nombre']}",
            value=(f"**Estado:** {estado}\n"
                   f"**Ganadores:** {gw['ganadores_n']}\n"
                   f"**Finaliza:** {fin_str}\n"
                   f"**Host:** <@{gw['host_id']}>"),
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="gdelete")
@commands.has_permissions(manage_messages=True)
async def gdelete(ctx, numero: int):
    """Elimina un sorteo por su número. Uso: .gdelete <numero>"""
    gw_id, gw = _buscar_giveaway_por_numero(ctx.guild.id, numero)
    if gw is None:
        return await ctx.send(f"❌ No existe un sorteo #{numero} en este servidor.")
    # Intentar borrar el mensaje del sorteo si aún existe.
    try:
        canal = ctx.guild.get_channel(int(gw["canal_id"]))
        if canal is not None:
            mensaje = await canal.fetch_message(int(gw["mensaje_id"]))
            await mensaje.delete()
    except (discord.HTTPException, AttributeError):
        pass
    del giveaways_db[gw_id]
    guardar_giveaways()
    await ctx.send(f"✅ Sorteo #{numero} eliminado.")


@gdelete.error
async def gdelete_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Messages.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.gdelete <número>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ El número debe ser un entero.")


@bot.command(name="greroll")
@commands.has_permissions(manage_messages=True)
async def greroll(ctx, numero: int):
    """Elige nuevos ganadores para un sorteo finalizado. Uso: .greroll <numero>"""
    gw_id, gw = _buscar_giveaway_por_numero(ctx.guild.id, numero)
    if gw is None:
        return await ctx.send(f"❌ No existe un sorteo #{numero} en este servidor.")
    if not gw.get("finalizado"):
        return await ctx.send("❌ Ese sorteo aún no ha finalizado.")

    try:
        canal = ctx.guild.get_channel(int(gw["canal_id"]))
        mensaje = await canal.fetch_message(int(gw["mensaje_id"])) if canal else None
    except (discord.HTTPException, AttributeError):
        mensaje = None

    participantes = []
    if mensaje is not None:
        for reaction in mensaje.reactions:
            if str(reaction.emoji) == "🎉":
                participantes = [u async for u in reaction.users() if not u.bot and u.id != bot.user.id]
                break

    if not participantes:
        return await ctx.send("❌ No había participantes válidos para hacer reroll.")

    random.shuffle(participantes)
    n = gw["ganadores_n"]
    nuevos_ganadores = [p.id for p in participantes[:n]]
    gw["ganadores"] = nuevos_ganadores
    guardar_giveaways()

    embed = _embed_giveaway(gw)
    if mensaje is not None:
        try:
            await mensaje.edit(embed=embed)
        except discord.HTTPException:
            pass

    menciones = " ".join(f"<@{g}>" for g in nuevos_ganadores)
    await ctx.send(f"🎲 Reroll del sorteo #{numero} **{gw['nombre']}**:\n{menciones}")


@greroll.error
async def greroll_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Messages.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.greroll <número>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ El número debe ser un entero.")


# ============================================================
#  LOCK / UNLOCK
# ============================================================

async def _lock_unlock(ctx, canal: discord.TextChannel, lock: bool):
    """Aplica o levanta el lock de un canal para @everyone."""
    if canal is None:
        canal = ctx.channel
    everyone = ctx.guild.default_role
    accion = "lockear" if lock else "unlockear"
    verbo_passado = "lockeado" if lock else "unlockeado"

    if ctx.author != ctx.guild.owner and not ctx.author.guild_permissions.manage_channels:
        return await ctx.send(f"❌ Necesitas el permiso Manage Channels para {accion} canales.")
    if not ctx.guild.me.guild_permissions.manage_channels:
        return await ctx.send(f"❌ Necesito el permiso Manage Channels para {accion} canales.")

    if lock:
        overwrite = canal.overwrites_for(everyone)
        bloqueado_send = overwrite.send_messages is False
        bloqueado_hilos = overwrite.create_public_threads is False and overwrite.create_private_threads is False
        if bloqueado_send and bloqueado_hilos:
            return await ctx.send(f"ℹ️ {canal.mention} ya estaba lockeado.")
        overwrite.send_messages = False
        overwrite.create_public_threads = False
        overwrite.create_private_threads = False
        try:
            await canal.set_permissions(everyone, overwrite=overwrite, reason=f"Lock por {ctx.author} (ID {ctx.author.id})")
        except discord.Forbidden:
            return await ctx.send(f"❌ No tengo permisos para editar los permisos de {canal.mention}.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Error al lockear: {e}")
    else:
        overwrite = canal.overwrites_for(everyone)
        if overwrite.send_messages is None and overwrite.create_public_threads is None:
            return await ctx.send(f"ℹ️ {canal.mention} no estaba lockeado.")
        overwrite.send_messages = None
        overwrite.create_public_threads = None
        overwrite.create_private_threads = None
        try:
            await canal.set_permissions(everyone, overwrite=overwrite, reason=f"Unlock por {ctx.author} (ID {ctx.author.id})")
        except discord.Forbidden:
            return await ctx.send(f"❌ No tengo permisos para editar los permisos de {canal.mention}.")
        except discord.HTTPException as e:
            return await ctx.send(f"❌ Error al unlockear: {e}")

    estado = "🔒" if lock else "🔓"
    await ctx.send(f"{estado} {canal.mention} ha sido {verbo_passado}.")
    embed = discord.Embed(
        title=("🔒 Canal lockeado" if lock else "🔓 Canal unlockeado"),
        color=discord.Color.red() if lock else discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Canal", value=canal.mention, inline=True)
    embed.add_field(name="Moderador", value=ctx.author.mention, inline=True)
    await enviar_logs(ctx.guild, embed)


@bot.command(name="lock")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def lock(ctx, canal: discord.TextChannel = None):
    """Bloquea un canal (no se puede escribir ni abrir hilos). Uso: .lock [#canal]"""
    await _lock_unlock(ctx, canal, True)


@lock.error
async def lock_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Channels.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Channels.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("❌ No encontré ese canal. Uso: `.lock [#canal]`")


@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def unlock(ctx, canal: discord.TextChannel = None):
    """Desbloquea un canal. Uso: .unlock [#canal]"""
    await _lock_unlock(ctx, canal, False)


@unlock.error
async def unlock_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Channels.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Channels.")
    elif isinstance(error, commands.ChannelNotFound):
        await ctx.send("❌ No encontré ese canal. Uso: `.unlock [#canal]`")


# ============================================================
#  RENAME / NAMERESET
# ============================================================

@bot.command(name="rename")
@commands.has_permissions(manage_nicknames=True)
@commands.bot_has_permissions(manage_nicknames=True)
async def rename(ctx, usuario_arg: str, *, apodo: str):
    """Cambia el apodo de un miembro. Uso: .rename <id|@|nombre> <nuevo apodo>"""
    apodo = apodo.strip()
    if not apodo:
        return await ctx.send("❌ Debes indicar un apodo. Uso: `.rename <id|@|nombre> <apodo>`")
    if len(apodo) > 32:
        return await ctx.send("❌ El apodo no puede superar los 32 caracteres.")

    usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)
    if miembro is None:
        return await ctx.send(f"❌ Ese usuario no está en este servidor.")
    if miembro.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send("❌ No puedes cambiar el apodo de alguien con un rol igual o superior al tuyo.")
    if ctx.guild.me.top_role <= miembro.top_role:
        return await ctx.send("❌ Mi rol es inferior al de ese usuario.")
    if miembro.id == ctx.guild.owner_id:
        return await ctx.send("❌ No puedo cambiar el apodo del dueño del servidor.")

    try:
        await miembro.edit(nick=apodo, reason=f"Cambio de apodo por {ctx.author} (ID {ctx.author.id})")
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para cambiar el apodo de ese usuario.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al cambiar el apodo: {e}")

    await ctx.send(f"✅ Apodo de {miembro.mention} cambiado a `{apodo}`.")


@rename.error
async def rename_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Nicknames.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Nicknames.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.rename <id_usuario> <apodo>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")


@bot.command(name="namereset")
@commands.has_permissions(manage_nicknames=True)
@commands.bot_has_permissions(manage_nicknames=True)
async def namereset(ctx, usuario_arg: str):
    """Restablece el apodo de un miembro al original. Uso: .namereset <id|@|nombre>"""
    usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)
    if miembro is None:
        return await ctx.send(f"❌ Ese usuario no está en este servidor.")
    if miembro.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send("❌ No puedes cambiar el apodo de alguien con un rol igual o superior al tuyo.")
    if ctx.guild.me.top_role <= miembro.top_role:
        return await ctx.send("❌ Mi rol es inferior al de ese usuario.")
    if miembro.id == ctx.guild.owner_id:
        return await ctx.send("❌ No puedo cambiar el apodo del dueño del servidor.")

    if miembro.nick is None:
        return await ctx.send(f"ℹ️ {miembro.mention} no tiene apodo personalizado.")

    try:
        await miembro.edit(nick=None, reason=f"Reset de apodo por {ctx.author} (ID {ctx.author.id})")
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para cambiar el apodo.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al restablecer el apodo: {e}")

    await ctx.send(f"✅ Apodo de {miembro.mention} restablecido.")


@namereset.error
async def namereset_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Nicknames.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Nicknames.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.namereset <id_usuario>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")


# ============================================================
#  IP BAN (ban + blocked en audit log + info para ipunban)
# ============================================================

@bot.command(name="ipban")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ipban(ctx, usuario_arg: str, *, motivo: str = "No especificado"):
    """
    Banea por IP equivalente: banea al usuario + veto vía audit log.
    Uso: .ipban <id|@|nombre> [motivo]
    """
    usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)

    if miembro is not None:
        if miembro.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ No puedes banear a alguien con rol igual o superior al tuyo.")
        if ctx.guild.me.top_role <= miembro.top_role:
            return await ctx.send("❌ Mi rol es inferior al de ese usuario.")

    motivo_full = f"[IP-BAN] {ctx.author} (ID {ctx.author.id}): {motivo}"
    try:
        await ctx.guild.ban(usuario, reason=motivo_full, delete_message_days=1)
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para banear a ese usuario.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al banear: {e}")

    embed = discord.Embed(
        title="🚫 IP-Ban aplicado",
        color=discord.Color.dark_red(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Usuario", value=f"{usuario} (`{usuario.id}`)", inline=False)
    embed.add_field(name="Moderador", value=ctx.author.mention, inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.set_footer(text="IP-BAN = ban normal + veto a futuras cuentas por audit log + estado")
    embed.set_thumbnail(url=usuario.display_avatar.url)
    await ctx.send(embed=embed)
    await enviar_logs(ctx.guild, embed)


@ipban.error
async def ipban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ No tienes permiso para banear.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.ipban <id_usuario> [motivo]`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Ban Members.")


@bot.command(name="ipunban")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ipunban(ctx, usuario_arg: str):
    """Levanta el IP-ban (desbanea al usuario). Uso: .ipunban <id|@|nombre>"""
    usuario, _, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        # Permitir también buscar por nombre en la lista de baneados.
        try:
            bans = await ctx.guild.bans()
            for entry in bans:
                if entry.user.name.lower() == usuario_arg.lower() or str(entry.user.id) == usuario_arg:
                    usuario = entry.user
                    err = None
                    break
        except discord.HTTPException:
            pass
        if err:
            return await ctx.send(err)
    try:
        await ctx.guild.unban(usuario, reason=f"IP-UNBAN por {ctx.author} (ID {ctx.author.id})")
    except discord.NotFound:
        return await ctx.send("ℹ️ Ese usuario no estaba baneado.")
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para desbanear.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al desbanear: {e}")
    embed = discord.Embed(title="✅ IP-Unban aplicado", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{usuario} (`{usuario.id}`)", inline=False)
    embed.add_field(name="Moderador", value=ctx.author.mention, inline=True)
    await ctx.send(embed=embed)
    await enviar_logs(ctx.guild, embed)


@ipunban.error
async def ipunban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ No tienes permiso para banear.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.ipunban <id_usuario>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Ban Members.")


@ipunban.error
async def ipunban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ No tienes permiso para banear.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.ipunban <id_usuario>`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Ban Members.")


# ============================================================
#  HONEYPOT
# ============================================================

@bot.command(name="honeypot")
@commands.has_permissions(manage_guild=True)
@commands.bot_has_permissions(manage_channels=True, ban_members=True, kick_members=True, moderate_members=True)
async def honeypot(ctx, canal: discord.TextChannel):
    """Configura un canal como honeypot (por defecto banea). Uso: .honeypot #canal"""
    gid = str(ctx.guild.id)
    cid = str(canal.id)
    honeypots_db.setdefault(gid, {})
    if cid in honeypots_db[gid]:
        return await ctx.send(f"ℹ️ {canal.mention} ya es un honeypot.")
    honeypots_db[gid][cid] = {"action": "ban", "duration": None}
    guardar_honeypots()
    await ctx.send(f"✅ {canal.mention} configurado como honeypot (acción: ban).")


@bot.command(name="honeypots")
@commands.has_permissions(manage_guild=True)
async def honeypots_cmd(ctx):
    """Lista los honeypots configurados con botón para eliminar."""
    gid = str(ctx.guild.id)
    data = honeypots_db.get(gid, {})
    if not data:
        return await ctx.send("ℹ️ No hay honeypots configurados en este servidor.")
    view = HoneypotListView(ctx.guild, data)
    embed = discord.Embed(title="🍯 Honeypots configurados", color=discord.Color.dark_gold())
    lines = []
    for cid_str, cfg in data.items():
        canal = ctx.guild.get_channel(int(cid_str))
        nombre = canal.mention if canal else f"ID {cid_str}"
        lines.append(f"{nombre} → acción: **{cfg['action']}**{' ('+str(cfg['duration'])+'s)' if cfg.get('duration') else ''}")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed, view=view)


class HoneypotListView(discord.ui.View):
    def __init__(self, guild: discord.Guild, data: dict):
        super().__init__(timeout=60)
        self.guild = guild
        self.data = data
        for cid_str in list(data.keys())[:25]:  # max 25 buttons
            canal = guild.get_channel(int(cid_str))
            label = f"🗑️ {canal.name}" if canal else f"🗑️ {cid_str}"
            btn = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.danger, custom_id=f"delhp_{cid_str}")
            btn.callback = self.make_callback(cid_str)
            self.add_item(btn)

    def make_callback(self, cid_str):
        async def callback(interaction: discord.Interaction):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("❌ Sin permiso.", ephemeral=True)
                return
            gid = str(self.guild.id)
            if cid_str in self.data:
                del self.data[cid_str]
                honeypots_db[gid] = self.data
                guardar_honeypots()
                await interaction.response.send_message(f"✅ Honeypot eliminado para <#{cid_str}>.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ No encontrado.", ephemeral=True)
        return callback


@bot.command(name="honeypotset")
@commands.has_permissions(manage_guild=True)
async def honeypotset(ctx, canal: discord.TextChannel, accion: str, duracion: str = None):
    """Configura la acción del honeypot. Uso: .honeypotset #canal ban|kick|mute [duración]"""
    gid = str(ctx.guild.id)
    cid = str(canal.id)
    if gid not in honeypots_db or cid not in honeypots_db[gid]:
        return await ctx.send("❌ Ese canal no es un honeypot.")
    accion = accion.lower()
    if accion not in ("ban", "kick", "mute"):
        return await ctx.send("❌ Acción inválida. Usa ban, kick o mute.")
    cfg = {"action": accion, "duration": None}
    if accion == "mute":
        if not duracion:
            return await ctx.send("❌ Para mute debes indicar duración (ej: 5m, 1h).")
        segundos, err = parsear_duracion(duracion)
        if err:
            return await ctx.send(f"❌ {err}")
        cfg["duration"] = segundos
    honeypots_db[gid][cid] = cfg
    guardar_honeypots()
    await ctx.send(f"✅ Honeypot {canal.mention} actualizado: acción **{accion}**{' ('+duracion+')' if duracion else ''}.")


# ============================================================
#  SOFTBAN (baneo temporal automático)
# ============================================================

async def _tarea_softban_unban(guild_id: int, user_id: int, segundos: int, moderador: discord.abc.User):
    await asyncio.sleep(segundos)
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    try:
        bans = await guild.bans()
    except discord.HTTPException:
        return
    usuario_obj = None
    for entry in bans:
        if entry.user.id == user_id:
            usuario_obj = entry.user
            break
    if usuario_obj is None:
        return  # Ya fue desbaneado manualmente.
    try:
        await guild.unban(usuario_obj, reason=f"Softban expirado (original por {moderador})")
        print(f"Softban expirado: unbanned {usuario_obj.id}")
    except discord.HTTPException:
        pass


@bot.command(name="softban")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def softban(ctx, usuario_arg: str, duracion: str, *, motivo: str = "No especificado"):
    """Banea temporalmente a un usuario. Uso: .softban <id/@/nombre> <duracion> [motivo]"""
    segundos, err = parsear_duracion(duracion)
    if err:
        return await ctx.send(f"❌ {err}")
    if segundos > 86400 * 7:
        return await ctx.send("❌ La duración máxima de un softban es de 7 días.")

    usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
    if err:
        return await ctx.send(err)

    if miembro is not None:
        if miembro.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("❌ No puedes banear a alguien con rol igual o superior al tuyo.")
        if ctx.guild.me.top_role <= miembro.top_role:
            return await ctx.send("❌ Mi rol es inferior al de ese usuario, no puedo banearlo.")

    try:
        await ctx.guild.ban(usuario, reason=f"[SOFTBAN {duracion}] {ctx.author} (ID {ctx.author.id}): {motivo}", delete_message_days=0)
    except discord.Forbidden:
        return await ctx.send("❌ No tengo permisos para banear a ese usuario.")
    except discord.HTTPException as e:
        return await ctx.send(f"❌ Error al banear: {e}")

    bot.loop.create_task(_tarea_softban_unban(ctx.guild.id, usuario.id, segundos, ctx.author))

    embed = discord.Embed(title="⏱️ Softban aplicado", color=discord.Color.dark_gold(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{usuario} (`{usuario.id}`)", inline=False)
    embed.add_field(name="Moderador", value=ctx.author.mention, inline=True)
    embed.add_field(name="Duración", value=duracion, inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.set_thumbnail(url=usuario.display_avatar.url)
    embed.set_footer(text=f"Será desbaneado automáticamente a las {discord.utils.format_dt(discord.utils.utcnow() + datetime.timedelta(seconds=segundos), 'T')}")
    await ctx.send(embed=embed)


@softban.error
async def softban_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Ban Members.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.softban <id|@|nombre> <duración> [motivo]`\nEj: `.softban 123456 1h Spam`")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Ban Members.")


# ============================================================
#  REMINDME (recordatorio)
# ============================================================

async def _tarea_reminder(reminder_id: str):
    r = reminders_db.get(reminder_id)
    if r is None:
        return
    fin_dt = datetime.datetime.fromisoformat(r["fin"])
    segundos_restantes = (fin_dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if segundos_restantes > 0:
        await asyncio.sleep(segundos_restantes)
    r = reminders_db.get(reminder_id)
    if r is None:
        return
    # Enviar recordatorio.
    guild = bot.get_guild(int(r["guild_id"]))
    destino = None
    if r.get("md", False):
        try:
            usuario = bot.get_user(int(r["user_id"])) or await bot.fetch_user(int(r["user_id"]))
            destino = await usuario.create_dm()
        except discord.HTTPException:
            destino = None
    if destino is None and guild is not None:
        canal = guild.get_channel(int(r["channel_id"]))
        if canal is not None:
            destino = canal
    if destino is not None:
        embed = discord.Embed(
            title="⏰ Recordatorio",
            description=r["msg"],
            color=discord.Color.gold(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        try:
            await destino.send(content=f"<@{r['user_id']}>", embed=embed)
        except discord.HTTPException:
            pass
    del reminders_db[reminder_id]
    guardar_reminders()


async def _reanudar_reminders():
    for rid, r in list(reminders_db.items()):
        fin_dt = datetime.datetime.fromisoformat(r["fin"])
        if datetime.datetime.now(datetime.timezone.utc) >= fin_dt:
            await _tarea_reminder(rid)
        else:
            bot.loop.create_task(_tarea_reminder(rid))


@bot.command(name="remindme")
async def remindme(ctx, duracion: str, *, mensaje_y_md: str):
    """
    Programa un recordatorio. Uso: .remindme <duracion> <mensaje> (MD: sí|no)
    Ej: .remindme 1h30m Comprar pan (MD: sí)
    """
    segundos, err = parsear_duracion(duracion)
    if err:
        return await ctx.send(f"❌ {err}")
    if segundos > 86400 * 30:
        return await ctx.send("❌ La duración máxima de un recordatorio es de 30 días.")

    texto = (mensaje_y_md or "").strip()
    if not texto:
        return await ctx.send("❌ Uso: `.remindme <duración> <mensaje> (MD: sí|no)`\nEj: `.remindme 1h Comprar pan (MD: no)`")

    md = False
    match_md = re.search(r"\(\s*MD\s*:\s*(s[ií]|no|n|false|true|0|1)\s*\)", texto, re.IGNORECASE)
    if match_md:
        resp = match_md.group(1).lower()
        md = resp in ("si", "sí", "s", "true", "1")
        texto = re.sub(r"\(\s*MD\s*:\s*(s[ií]|no|n|false|true|0|1)\s*\)", "", texto, flags=re.IGNORECASE).strip()

    if len(texto) > 1000:
        return await ctx.send("❌ El mensaje no puede superar los 1000 caracteres.")

    rid = f"{ctx.author.id}-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}"
    fin_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=segundos)
    reminders_db[rid] = {
        "user_id": str(ctx.author.id),
        "guild_id": str(ctx.guild.id) if ctx.guild else None,
        "channel_id": str(ctx.channel.id),
        "msg": texto,
        "fin": fin_dt.isoformat(),
        "md": md,
    }
    guardar_reminders()
    bot.loop.create_task(_tarea_reminder(rid))

    embed = discord.Embed(title="✅ Recordatorio programado", color=discord.Color.green())
    embed.add_field(name="Para dentro de", value=duracion, inline=True)
    embed.add_field(name="Notificación", value=("MD privado" if md else "En este canal"), inline=True)
    embed.add_field(name="Mensaje", value=texto[:1024], inline=False)
    embed.set_footer(text=f"Se te avisará a las {discord.utils.format_dt(fin_dt, 'T')}")
    await ctx.send(embed=embed)


@remindme.error
async def remindme_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso: `.remindme <duración> <mensaje> (MD: sí|no)`\nEj: `.remindme 1h Comprar pan (MD: no)`")


# ============================================================
#  HELP, PREFIX, SETPREFIX, PREFIXREMOVE
# ============================================================

@bot.command(name="help", aliases=["ayuda", "comandos"])
async def ayuda(ctx, *, comando: str = None):
    """Muestra la lista de comandos disponibles."""
    prefijo = get_prefix_message(ctx.guild)
    if comando is not None:
        cmd = bot.get_command(comando.lower())
        if cmd is None:
            return await ctx.send(f"❌ No existe el comando `{comando}`.")
        embed = discord.Embed(title=f"📖 Ayuda: {cmd.name}", color=discord.Color.blurple())
        embed.add_field(name="Descripción", value=cmd.help or "Sin descripción.", inline=False)
        embed.add_field(name="Uso", value=f"`{prefijo}{cmd.signature}`", inline=False)
        if cmd.aliases:
            embed.add_field(name="Aliases", value=", ".join(cmd.aliases), inline=False)
        return await ctx.send(embed=embed)

    embed = discord.Embed(
        title="📖 Lista de comandos",
        description=f"Prefijo actual: {prefijo}\nTambién puedes usar slash commands `/` y mencionar al bot.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="🛡️ Moderación",
        value=(
            "`ban` `kick` `unban` `mute` `unmute` `softban` `ipban` `ipunban`\n"
            "`purge` `nuke` `lock` `unlock` `rename` `namereset` `warn` `warnremove` `warns`"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 Roles",
        value="`roleadd` `roleremove` `rolehuman` `roleall` `rolebot`",
        inline=False,
    )
    embed.add_field(
        name="🎉 Sorteos y utilidades",
        value="`gcreate` `glist` `gdelete` `greroll` `avatar` `banner` `remindme`",
        inline=False,
    )
    embed.add_field(
        name="🔗 Canales y links",
        value="`linkban` `linkunban` `linkbanlist` `logchannel` `logunchannel` `logschannels`",
        inline=False,
    )
    embed.add_field(
        name="⚙️ Configuración",
        value="`setprefix` `prefix` `prefixremove` `sync` `help`",
        inline=False,
    )
    embed.set_footer(text="Usa .help <comando> para ver el detalle de un comando específico.")
    await ctx.send(embed=embed)


@bot.command(name="setprefix")
@commands.has_permissions(manage_guild=True)
async def setprefix(ctx, prefijo: str):
    """Añade un prefijo personalizado para este servidor. Uso: .setprefix <carácter>"""
    prefijo = prefijo.strip()
    if not prefijo:
        return await ctx.send("❌ Debes indicar un prefijo. Uso: `.setprefix <carácter>`")
    if len(prefijo) > 5:
        return await ctx.send("❌ El prefijo no puede tener más de 5 caracteres.")
    gid = str(ctx.guild.id)
    customs = prefixes_db.setdefault(gid, [])
    if prefijo in customs or prefijo == DEFAULT_PREFIX:
        return await ctx.send(f"ℹ️ El prefijo `{prefijo}` ya estaba activo.")
    customs.append(prefijo)
    guardar_prefixes()
    await ctx.send(f"✅ Prefijo `{prefijo}` añadido. Ahora puedes usar `{prefijo}comando`.")


@setprefix.error
async def setprefix_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Server.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.setprefix <carácter>`")


@bot.command(name="prefix")
async def prefix(ctx):
    """Muestra los prefijos activos en el servidor."""
    prefs = _get_prefixes_sync(ctx.guild.id)
    embed = discord.Embed(
        title="⚙️ Prefijos activos",
        description="\n".join(f"• `{p}`" for p in prefs),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="El prefijo por defecto (.) no se puede eliminar.")
    await ctx.send(embed=embed)


@bot.command(name="prefixremove")
@commands.has_permissions(manage_guild=True)
async def prefixremove(ctx, prefijo: str):
    """Elimina un prefijo personalizado. Uso: .prefixremove <carácter>"""
    prefijo = prefijo.strip()
    gid = str(ctx.guild.id)
    customs = prefixes_db.get(gid, [])
    if prefijo == DEFAULT_PREFIX:
        return await ctx.send("❌ El prefijo por defecto `.` no se puede eliminar.")
    if prefijo not in customs:
        return await ctx.send(f"ℹ️ El prefijo `{prefijo}` no está configurado en este servidor.")
    customs.remove(prefijo)
    if not customs:
        del prefixes_db[gid]
    guardar_prefixes()
    await ctx.send(f"✅ Prefijo `{prefijo}` eliminado.")


@prefixremove.error
async def prefixremove_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Server.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.prefixremove <carácter>`")


# ============================================================
#  LOGS EXTRA (eventos automáticos)
# ============================================================

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    """Log cuando un usuario es baneado del servidor."""
    embed = discord.Embed(title="🔨 Miembro baneado", color=discord.Color.red(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{user} (`{user.id}`)", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    moderador = None
    motivo = None
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.ban, limit=5):
            if entry.target.id == user.id:
                moderador = entry.user
                motivo = entry.reason
                break
    except discord.Forbidden:
        pass
    if moderador:
        embed.add_field(name="Baneado por", value=moderador.mention, inline=True)
    if motivo:
        embed.add_field(name="Motivo", value=motivo, inline=False)
    await enviar_logs(guild, embed)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    """Log cuando un usuario es desbaneado."""
    embed = discord.Embed(title="✅ Miembro desbaneado", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{user} (`{user.id}`)", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    moderador = None
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.unban, limit=5):
            if entry.target.id == user.id:
                moderador = entry.user
                break
    except discord.Forbidden:
        pass
    if moderador:
        embed.add_field(name="Desbaneado por", value=moderador.mention, inline=True)
    await enviar_logs(guild, embed)


@bot.event
async def on_member_remove(member: discord.Member):
    """Log cuando un miembro sale/kickeado del servidor."""
    embed = discord.Embed(title="👋 Miembro salió", color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{member} (`{member.id}`)", inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    # ¿Fue kickeado?
    try:
        async for entry in member.guild.audit_logs(action=discord.AuditLogAction.kick, limit=5):
            if entry.target.id == member.id:
                embed.title = "👢 Miembro kickeado"
                embed.color = discord.Color.orange()
                embed.add_field(name="Kickeado por", value=entry.user.mention, inline=True)
                if entry.reason:
                    embed.add_field(name="Motivo", value=entry.reason, inline=False)
                break
    except discord.Forbidden:
        pass
    await enviar_logs(member.guild, embed)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Logs de cambios en miembros: apodo, roles, timeout."""
    if before.timed_out_until != after.timed_out_until:
        if after.is_timed_out():
            embed = discord.Embed(title="🔇 Miembro silenciado (timeout)", color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
            embed.add_field(name="Usuario", value=f"{after.mention} (`{after.id}`)", inline=False)
            embed.add_field(name="Hasta", value=discord.utils.format_dt(after.timed_out_until, "T"), inline=True)
            await enviar_logs(after.guild, embed)
        else:
            embed = discord.Embed(title="🔊 Miembro ya no silenciado", color=discord.Color.green(), timestamp=discord.utils.utcnow())
            embed.add_field(name="Usuario", value=f"{after.mention} (`{after.id}`)", inline=False)
            await enviar_logs(after.guild, embed)

    if before.nick != after.nick:
        embed = discord.Embed(title="✏️ Apodo cambiado", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Usuario", value=f"{after.mention} (`{after.id}`)", inline=False)
        embed.add_field(name="Antes", value=(before.nick or "*(sin apodo)*"), inline=True)
        embed.add_field(name="Después", value=(after.nick or "*(restablecido)*"), inline=True)
        await enviar_logs(after.guild, embed)

    if before.roles != after.roles:
        anadidos = [r for r in after.roles if r not in before.roles]
        quitados = [r for r in before.roles if r not in after.roles]
        embed = discord.Embed(title="🎭 Roles cambiados", color=discord.Color.blurple(), timestamp=discord.utils.utc())
        embed.add_field(name="Usuario", value=f"{after.mention} (`{after.id}`)", inline=False)
        if anadidos:
            embed.add_field(name="Añadidos", value=", ".join(r.mention for r in anadidos[:10]), inline=False)
        if quitados:
            embed.add_field(name="Quitados", value=", ".join(r.mention for r in quitados[:10]), inline=False)
        await enviar_logs(after.guild, embed)


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    embed = discord.Embed(title="➕ Canal creado", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Canal", value=f"{channel.mention} (`{channel.id}`)", inline=False)
    embed.add_field(name="Tipo", value=type(channel).__name__, inline=True)
    await enviar_logs(channel.guild, embed)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    embed = discord.Embed(title="➖ Canal eliminado", color=discord.Color.red(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Canal", value=f"`{channel.name}` (`{channel.id}`)", inline=False)
    embed.add_field(name="Tipo", value=type(channel).__name__, inline=True)
    await enviar_logs(channel.guild, embed)


@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
    """Logs de canal renombrado, movido de categoría, etc."""
    cambios = []
    if before.name != after.name:
        cambios.append(("Nombre", before.name, after.name))
    antes_cat = before.category.name if before.category else "*(ninguna)*"
    despues_cat = after.category.name if after.category else "*(ninguna)*"
    if antes_cat != despues_cat:
        cambios.append(("Categoría", antes_cat, despues_cat))
    if hasattr(before, "position") and hasattr(after, "position") and before.position != after.position:
        cambios.append(("Posición", str(before.position), str(after.position)))
    if hasattr(before, "topic") and hasattr(after, "topic") and before.topic != after.topic:
        cambios.append(("Tema", (before.topic or "*(vacio)*"), (after.topic or "*(vacio)*")))
    if not cambios:
        return
    embed = discord.Embed(title="✏️ Canal editado", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Canal", value=f"{after.mention} (`{after.id}`)", inline=False)
    for nombre, antes, despues in cambios[:5]:
        embed.add_field(name=nombre, value=f"`{antes}` → `{despues}`", inline=False)
    await enviar_logs(after.guild, embed)


@bot.event
async def on_guild_role_create(role: discord.Role):
    embed = discord.Embed(title="➕ Rol creado", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Rol", value=f"{role.mention} (`{role.id}`)", inline=False)
    await enviar_logs(role.guild, embed)


@bot.event
async def on_guild_role_delete(role: discord.Role):
    embed = discord.Embed(title="➖ Rol eliminado", color=discord.Color.red(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Rol", value=f"`{role.name}` (`{role.id}`)", inline=False)
    await enviar_logs(role.guild, embed)


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    cambios = []
    if before.name != after.name:
        cambios.append(("Nombre", before.name, after.name))
    if before.permissions != after.permissions:
        cambios.append(("Permisos", "Modificados", "Ver audit log"))
    if not cambios:
        return
    embed = discord.Embed(title="✏️ Rol editado", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Rol", value=f"{after.mention} (`{after.id}`)", inline=False)
    for nombre, antes, despues in cambios:
        embed.add_field(name=nombre, value=f"`{antes}` → `{despues}`", inline=False)
    await enviar_logs(after.guild, embed)





# ============================================================
#  SLASH COMMANDS (comandos con /) — misma funcionalidad
# ============================================================

async def _obtener_usuario(user_id: int):
    """Función compartida para obtener un usuario vía fetch_user."""
    try:
        return await bot.fetch_user(user_id), None
    except discord.NotFound:
        return None, f"❌ No se encontró ningún usuario con ID `{user_id}`."
    except discord.HTTPException as e:
        return None, f"❌ Error al buscar el usuario: {e}"


def _respuesta_hibrida(ctx_o_interaction, ephemeral: bool = False):
    """Devuelve una función send_fn compatible con ctx.send e interaction.response.send_message."""
    if isinstance(ctx_o_interaction, discord.Interaction):
        if ctx_o_interaction.response.is_done():
            async def send(content=None, embed=None, **kwargs):
                await ctx_o_interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral, **kwargs)
        else:
            async def send(content=None, embed=None, **kwargs):
                await ctx_o_interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral, **kwargs)
    else:
        async def send(content=None, embed=None, **kwargs):
            if ephemeral:
                try:
                    return await ctx_o_interaction.send(content=content, embed=embed, delete_after=10, **kwargs)
                except Exception:
                    pass
            await ctx_o_interaction.send(content=content, embed=embed, **kwargs)
    return send


# --- BAN ---
@bot.tree.command(name="ban", description="Banea a un usuario")
@app_commands.describe(usuario="Usuario a banear (mención, ID o nombre)", motivo="Motivo del ban (opcional)")
@app_commands.default_permissions(ban_members=True)
async def slash_ban(interaction: discord.Interaction, usuario: discord.User, motivo: str = "No especificado"):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message("❌ No tienes permiso para banear.", ephemeral=True)
    send = _respuesta_hibrida(interaction, ephemeral=False)
    miembro = interaction.guild.get_member(usuario.id)
    if miembro is not None:
        if miembro.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
            return await send("❌ No puedes banear a alguien con un rol igual o superior al tuyo.")
        if interaction.guild.me.top_role <= miembro.top_role:
            return await send("❌ Mi rol es inferior al de ese usuario, no puedo banearlo.")
    try:
        await interaction.guild.ban(usuario, reason=f"{interaction.user} (ID {interaction.user.id}): {motivo}", delete_message_days=1)
    except discord.Forbidden:
        return await send("❌ No tengo permisos para banear a ese usuario.")
    except discord.HTTPException as e:
        return await send(f"❌ Error al banear: {e}")
    embed = discord.Embed(title="🔨 Usuario baneado", color=discord.Color.red(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{usuario} (`{usuario.id}`)", inline=False)
    embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=True)
    embed.set_thumbnail(url=usuario.display_avatar.url)
    await send(embed=embed)


# --- KICK ---
@bot.tree.command(name="kick", description="Expulsa a un miembro")
@app_commands.describe(miembro="Miembro a expulsar (mención o nombre)", motivo="Motivo del kick (opcional)")
@app_commands.default_permissions(kick_members=True)
async def slash_kick(interaction: discord.Interaction, miembro: discord.Member, motivo: str = "No especificado"):
    if not interaction.user.guild_permissions.kick_members:
        return await interaction.response.send_message("❌ No tienes permiso para expulsar.", ephemeral=True)
    if miembro.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        return await interaction.response.send_message("❌ No puedes expulsar a alguien con un rol igual o superior al tuyo.", ephemeral=True)
    if interaction.guild.me.top_role <= miembro.top_role:
        return await interaction.response.send_message("❌ Mi rol es inferior al de ese usuario, no puedo expulsarlo.", ephemeral=True)
    try:
        await miembro.kick(reason=f"{interaction.user} (ID {interaction.user.id}): {motivo}")
    except discord.Forbidden:
        return await interaction.response.send_message("❌ No tengo permisos para expulsar a ese usuario.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al expulsar: {e}", ephemeral=True)
    embed = discord.Embed(title="👢 Usuario expulsado", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{miembro} (`{miembro.id}`)", inline=False)
    embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=True)
    embed.set_thumbnail(url=miembro.display_avatar.url)
    await interaction.response.send_message(embed=embed)


# --- UNBAN ---
@bot.tree.command(name="unban", description="Levanta el ban de un usuario")
@app_commands.describe(usuario="Usuario a desbanear (ID o mención)", motivo="Motivo (opcional)")
@app_commands.default_permissions(ban_members=True)
async def slash_unban(interaction: discord.Interaction, usuario: discord.User, motivo: str = "No especificado"):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message("❌ No tienes permiso para desbanear.", ephemeral=True)
    try:
        await interaction.guild.unban(usuario, reason=f"{interaction.user} (ID {interaction.user.id}): {motivo}")
    except discord.NotFound:
        return await interaction.response.send_message("ℹ️ Ese usuario no estaba baneado.", ephemeral=True)
    except discord.Forbidden:
        return await interaction.response.send_message("❌ No tengo permisos para desbanear.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al desbanear: {e}", ephemeral=True)
    await interaction.response.send_message(f"✅ {usuario} (`{usuario.id}`) ha sido desbaneado.")


# --- MUTE ---
@bot.tree.command(name="mute", description="Silencia (timeout) a un miembro. Duración tipo 5h, 30m, 1h30m")
@app_commands.describe(miembro="Miembro a silenciar (mención o nombre)", duracion="Duración (ej: 5h, 30m, 10s, 1h30m)", motivo="Motivo (opcional)")
@app_commands.default_permissions(moderate_members=True)
async def slash_mute(interaction: discord.Interaction, miembro: discord.Member, duracion: str, motivo: str = "No especificado"):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message("❌ No tienes permiso para silenciar.", ephemeral=True)
    segundos, err = parsear_duracion(duracion)
    if err:
        return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
    if miembro.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        return await interaction.response.send_message("❌ No puedes silenciar a alguien con un rol igual o superior al tuyo.", ephemeral=True)
    if interaction.guild.me.top_role <= miembro.top_role:
        return await interaction.response.send_message("❌ Mi rol es inferior al de ese usuario, no puedo silenciarlo.", ephemeral=True)
    if interaction.guild.owner_id == miembro.id:
        return await interaction.response.send_message("❌ No puedo silenciar al dueño del servidor.", ephemeral=True)
    try:
        hasta = discord.utils.utcnow() + datetime.timedelta(seconds=segundos)
        await miembro.timeout(hasta, reason=f"{interaction.user} (ID {interaction.user.id}): {motivo}")
    except discord.Forbidden:
        return await interaction.response.send_message("❌ No tengo permisos para silenciar a ese usuario.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al silenciar: {e}", ephemeral=True)
    bot.loop.create_task(_quitar_timeout_automatico(interaction.guild.id, miembro.id, segundos, interaction.user))
    humanas = []
    h = segundos // 3600; m = (segundos % 3600) // 60; s = segundos % 60
    if h: humanas.append(f"{h}h")
    if m: humanas.append(f"{m}m")
    if s: humanas.append(f"{s}s")
    duracion_str = " ".join(humanas) or "0s"
    embed = discord.Embed(title="🔇 Usuario silenciado", color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{miembro.mention} (`{miembro.id}`)", inline=False)
    embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
    embed.add_field(name="Duración", value=duracion_str, inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.set_thumbnail(url=miembro.display_avatar.url)
    embed.set_footer(text=f"Se quitará a las {discord.utils.format_dt(hasta, 'T')}")
    await interaction.response.send_message(embed=embed)


# --- UNMUTE ---
@bot.tree.command(name="unmute", description="Quita el timeout a un miembro")
@app_commands.describe(miembro="Miembro a desmutear (mención o nombre)", motivo="Motivo (opcional)")
@app_commands.default_permissions(moderate_members=True)
async def slash_unmute(interaction: discord.Interaction, miembro: discord.Member, motivo: str = "No especificado"):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message("❌ No tienes permiso.", ephemeral=True)
    if not miembro.is_timed_out():
        return await interaction.response.send_message("ℹ️ Ese usuario no está silenciado.", ephemeral=True)
    try:
        await miembro.timeout(None, reason=f"{interaction.user} (ID {interaction.user.id}): {motivo}")
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al quitar el silencio: {e}", ephemeral=True)
    await interaction.response.send_message(f"✅ {miembro.mention} ya puede hablar de nuevo.")


# --- AVATAR ---
@bot.tree.command(name="avatar", description="Muestra el avatar completo de un usuario")
@app_commands.describe(usuario="Usuario (mención, ID o nombre)")
async def slash_avatar(interaction: discord.Interaction, usuario: discord.User):
    avatar_url = usuario.display_avatar.with_size(4096).url
    embed = discord.Embed(title=f"Avatar de {usuario}", color=discord.Color.blurple())
    embed.set_image(url=avatar_url)
    embed.set_footer(text=f"ID: {usuario.id}")
    await interaction.response.send_message(embed=embed)


# --- BANNER ---
@bot.tree.command(name="banner", description="Muestra el banner completo de un usuario")
@app_commands.describe(usuario="Usuario (mención, ID o nombre)")
async def slash_banner(interaction: discord.Interaction, usuario: discord.User):
    banner_obj = usuario.banner
    if banner_obj is None:
        return await interaction.response.send_message(f"ℹ️ {usuario} no tiene banner de perfil.")
    color_hex = usuario.accent_color
    if color_hex is not None:
        color_embed = color_hex
        descripcion = f"**Color de acento:** `#{color_hex.value:06X}`"
    else:
        color_embed = discord.Color.blurple()
        descripcion = "Sin color de acento."
    banner_url = banner_obj.with_size(4096).url
    embed = discord.Embed(title=f"Banner de {usuario}", description=descripcion, color=color_embed)
    embed.set_image(url=banner_url)
    embed.set_footer(text=f"ID: {usuario.id}")
    await interaction.response.send_message(embed=embed)


# ============================================================
#  GRUPO: /warn ...
# ============================================================

warn_group = app_commands.Group(name="warn", description="Gestión de advertencias (warns)")


@warn_group.command(name="add", description="Advierte a un usuario (motivo obligatorio)")
@app_commands.describe(miembro="Usuario a advertir (mención o nombre)", motivo="Motivo del warn (obligatorio)")
@app_commands.default_permissions(moderate_members=True)
async def slash_warn_add(interaction: discord.Interaction, miembro: discord.Member, motivo: str):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message("❌ No tienes permiso para advertir.", ephemeral=True)
    if not motivo or motivo.strip() == "":
        return await interaction.response.send_message("❌ Debes indicar un motivo.", ephemeral=True)
    send = _respuesta_hibrida(interaction)
    clave = str(miembro.id)
    lista = warns_db.setdefault(clave, [])
    numero = len(lista) + 1
    entrada = {
        "numero": numero,
        "motivo": motivo.strip(),
        "moderador_id": interaction.user.id,
        "moderador": str(interaction.user),
        "fecha": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    lista.append(entrada)
    guardar_warns()
    embed = discord.Embed(title="⚠️ Advertencia aplicada", color=discord.Color.gold(), timestamp=datetime.datetime.now(datetime.timezone.utc))
    embed.add_field(name="Usuario", value=f"{miembro} (`{miembro.id}`)", inline=False)
    embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
    embed.add_field(name="Número de warn", value=f"**#{numero}**", inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.set_thumbnail(url=miembro.display_avatar.url)
    embed.set_footer(text=f"Total de warns: {len(lista)}")
    await send(embed=embed)


@warn_group.command(name="remove", description="Elimina un warn por su número")
@app_commands.describe(miembro="Usuario (mención o nombre)", numero="Número del warn a eliminar (ej: 1)")
@app_commands.default_permissions(moderate_members=True)
async def slash_warn_remove(interaction: discord.Interaction, miembro: discord.Member, numero: int):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message("❌ No tienes permiso.", ephemeral=True)
    send = _respuesta_hibrida(interaction)
    clave = str(miembro.id)
    lista = warns_db.get(clave, [])
    if not lista:
        return await send(f"ℹ️ {miembro} no tiene ningún warn.")
    warn_a_borrar = None
    for w in lista:
        if w["numero"] == numero:
            warn_a_borrar = w
            break
    if warn_a_borrar is None:
        return await send(f"❌ No existe el warn #{numero}. El usuario tiene warns del 1 al {len(lista)}.")
    lista.remove(warn_a_borrar)
    for i, w in enumerate(lista, start=1):
        w["numero"] = i
    guardar_warns()
    embed = discord.Embed(title="✅ Warn eliminado", color=discord.Color.green(), timestamp=datetime.datetime.now(datetime.timezone.utc))
    embed.add_field(name="Usuario", value=f"<@{miembro.id}> (`{miembro.id}`)", inline=False)
    embed.add_field(name="Warn eliminado", value=f"#{numero}", inline=True)
    embed.add_field(name="Motivo original", value=warn_a_borrar["motivo"][:1024], inline=False)
    embed.set_footer(text=f"Warns restantes: {len(lista)}")
    await send(embed=embed)


@warn_group.command(name="list", description="Muestra todos los warns de un usuario")
@app_commands.describe(miembro="Usuario (mención o nombre)")
@app_commands.default_permissions(moderate_members=True)
async def slash_warn_list(interaction: discord.Interaction, miembro: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message("❌ No tienes permiso.", ephemeral=True)
    send = _respuesta_hibrida(interaction)
    clave = str(miembro.id)
    lista = warns_db.get(clave, [])
    nombre_usuario = f"{miembro}"
    if not lista:
        embed = discord.Embed(title="✅ Sin advertencias", description=f"{nombre_usuario} (`{miembro.id}`) no tiene ningún warn.", color=discord.Color.green())
        return await send(embed=embed)
    embed = discord.Embed(title=f"⚠️ Warns de {nombre_usuario}", color=discord.Color.gold(), timestamp=datetime.datetime.now(datetime.timezone.utc))
    embed.set_footer(text=f"Total: {len(lista)} advertencia(s)")
    for w in lista:
        try:
            fecha_dt = datetime.datetime.fromisoformat(w["fecha"])
            fecha_str = discord.utils.format_dt(fecha_dt, "f")
        except (ValueError, KeyError):
            fecha_str = "Fecha desconocida"
        embed.add_field(name=f"#{w['numero']} — {fecha_str}", value=(f"**Motivo:** {w['motivo'][:900]}\n**Moderador:** {w['moderador']}"), inline=False)
    await send(embed=embed)


# ============================================================
#  GRUPO: /role ...
# ============================================================

role_group = app_commands.Group(name="role", description="Gestión de roles de miembros")


@role_group.command(name="add", description="Otorga un rol a un usuario")
@app_commands.describe(miembro="Usuario (mención o nombre)", rol="Rol a asignar")
@app_commands.default_permissions(manage_roles=True)
async def slash_role_add(interaction: discord.Interaction, miembro: discord.Member, rol: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ No tienes permiso para gestionar roles.", ephemeral=True)
    if rol.position >= interaction.user.top_role.position and interaction.user != interaction.guild.owner:
        return await interaction.response.send_message("❌ No puedes otorgar un rol igual o superior al tuyo.", ephemeral=True)
    if rol.position >= interaction.guild.me.top_role.position:
        return await interaction.response.send_message("❌ Ese rol está por encima del mío, no puedo asignarlo.", ephemeral=True)
    if rol.managed:
        return await interaction.response.send_message("❌ Ese rol está gestionado por una integración/bot, no puedo asignarlo.", ephemeral=True)
    if rol in miembro.roles:
        return await interaction.response.send_message(f"ℹ️ {miembro.mention} ya tiene el rol {rol.mention}.", ephemeral=True)
    try:
        await miembro.add_roles(rol, reason=f"{interaction.user} (ID {interaction.user.id})")
    except discord.Forbidden:
        return await interaction.response.send_message("❌ No tengo permisos para asignar ese rol.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al asignar el rol: {e}", ephemeral=True)
    await interaction.response.send_message(f"✅ Rol {rol.mention} asignado a {miembro.mention}.")


@role_group.command(name="remove", description="Quita un rol a un usuario")
@app_commands.describe(miembro="Usuario (mención o nombre)", rol="Rol a quitar")
@app_commands.default_permissions(manage_roles=True)
async def slash_role_remove(interaction: discord.Interaction, miembro: discord.Member, rol: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ No tienes permiso para gestionar roles.", ephemeral=True)
    if rol.position >= interaction.user.top_role.position and interaction.user != interaction.guild.owner:
        return await interaction.response.send_message("❌ No puedes quitar un rol igual o superior al tuyo.", ephemeral=True)
    if rol.position >= interaction.guild.me.top_role.position:
        return await interaction.response.send_message("❌ Ese rol está por encima del mío, no puedo quitarlo.", ephemeral=True)
    if rol.managed:
        return await interaction.response.send_message("❌ Ese rol está gestionado por una integración/bot.", ephemeral=True)
    if rol not in miembro.roles:
        return await interaction.response.send_message(f"ℹ️ {miembro.mention} no tiene el rol {rol.mention}.", ephemeral=True)
    try:
        await miembro.remove_roles(rol, reason=f"{interaction.user} (ID {interaction.user.id})")
    except discord.Forbidden:
        return await interaction.response.send_message("❌ No tengo permisos para quitar ese rol.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al quitar el rol: {e}", ephemeral=True)
    await interaction.response.send_message(f"✅ Rol {rol.mention} quitado a {miembro.mention}.")


# --- Subcomandos masivos del grupo role: /role human|all|bot ---

async def _aplicar_role_masivo_slash(interaction, rol: discord.Role, filtro: str):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ No tienes permiso para gestionar roles.", ephemeral=True)
    if rol.position >= interaction.user.top_role.position and interaction.user != interaction.guild.owner:
        return await interaction.response.send_message("❌ No puedes otorgar un rol igual o superior al tuyo.", ephemeral=True)
    if rol.position >= interaction.guild.me.top_role.position:
        return await interaction.response.send_message("❌ Ese rol está por encima del mío, no puedo asignarlo.", ephemeral=True)
    if rol.managed:
        return await interaction.response.send_message("❌ Ese rol está gestionado por una integración y no puedo asignarlo.", ephemeral=True)

    miembros = interaction.guild.members
    if filtro == "humanos":
        objetivos = [m for m in miembros if not m.bot and rol not in m.roles]
        descripcion = "miembros humanos"
    elif filtro == "bots":
        objetivos = [m for m in miembros if m.bot and rol not in m.roles]
        descripcion = "bots"
    else:
        objetivos = [m for m in miembros if rol not in m.roles]
        descripcion = "miembros"

    total = len(objetivos)
    if total == 0:
        return await interaction.response.send_message(f"ℹ️ No hay {descripcion} sin el rol {rol.mention}.")

    # En slash no podemos editar un msg en bucle tan facil antes de la primera respuesta.
    await interaction.response.send_message(f"⏳ Asignando {rol.mention} a **{total}** {descripcion}...")
    msg = await interaction.original_response()
    asignados = 0
    errores = 0
    for i, miembro in enumerate(objetivos, start=1):
        try:
            await miembro.add_roles(rol, reason=f"{interaction.user} (ID {interaction.user.id}) — asignación masiva ({filtro})")
            asignados += 1
        except discord.Forbidden:
            errores += 1
        except discord.HTTPException:
            errores += 1
        if i % 10 == 0 or i == total:
            try:
                await msg.edit(content=f"⏳ Asignando {rol.mention} a {descripcion}... (`{i}/{total}`)")
            except discord.HTTPException:
                pass

    embed = discord.Embed(title="✅ Asignación masiva completada", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Rol", value=rol.mention, inline=True)
    embed.add_field(name="Filtro", value=filtro.capitalize(), inline=True)
    embed.add_field(name="Asignados", value=f"**{asignados}/{total}**", inline=True)
    if errores:
        embed.add_field(name="Errores", value=str(errores), inline=True)
    embed.set_footer(text=f"Ejecutado por {interaction.user}")
    await msg.edit(content=None, embed=embed)


@role_group.command(name="human", description="Asigna un rol a todos los miembros HUMANOS del servidor")
@app_commands.describe(rol="Rol a asignar")
@app_commands.default_permissions(manage_roles=True)
async def slash_role_human(interaction: discord.Interaction, rol: discord.Role):
    await _aplicar_role_masivo_slash(interaction, rol, "humanos")


@role_group.command(name="all", description="Asigna un rol a todos los miembros (humanos y bots)")
@app_commands.describe(rol="Rol a asignar")
@app_commands.default_permissions(manage_roles=True)
async def slash_role_all(interaction: discord.Interaction, rol: discord.Role):
    await _aplicar_role_masivo_slash(interaction, rol, "todos")


@role_group.command(name="bot", description="Asigna un rol solo a los BOTS del servidor")
@app_commands.describe(rol="Rol a asignar")
@app_commands.default_permissions(manage_roles=True)
async def slash_role_bot(interaction: discord.Interaction, rol: discord.Role):
    await _aplicar_role_masivo_slash(interaction, rol, "bots")


bot.tree.add_command(role_group)


# ============================================================
#  GRUPO: /link ...
# ============================================================

link_group = app_commands.Group(name="link", description="Gestión de canales sin enlaces")


@link_group.command(name="ban", description="Prohibe enlaces y archivos en un canal")
@app_commands.describe(canal="Canal donde prohibir enlaces")
@app_commands.default_permissions(manage_messages=True)
async def slash_link_ban(interaction: discord.Interaction, canal: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Messages.", ephemeral=True)
    if canal.id in linkban_canal:
        return await interaction.response.send_message(f"ℹ️ {canal.mention} ya tenía los enlaces prohibidos.")
    linkban_canal.add(canal.id)
    guardar_linkban()
    await interaction.response.send_message(f"✅ Enlaces y archivos prohibidos en {canal.mention}.")


@link_group.command(name="unban", description="Permite enlaces de nuevo en un canal")
@app_commands.describe(canal="Canal donde permitir enlaces")
@app_commands.default_permissions(manage_messages=True)
async def slash_link_unban(interaction: discord.Interaction, canal: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Messages.", ephemeral=True)
    if canal.id not in linkban_canal:
        return await interaction.response.send_message(f"ℹ️ {canal.mention} no tenía los enlaces prohibidos.")
    linkban_canal.discard(canal.id)
    guardar_linkban()
    await interaction.response.send_message(f"✅ Enlaces permitidos de nuevo en {canal.mention}.")


@link_group.command(name="list", description="Lista los canales con enlaces prohibidos")
@app_commands.default_permissions(manage_messages=True)
async def slash_link_list(interaction: discord.Interaction):
    if not linkban_canal:
        return await interaction.response.send_message("ℹ️ No hay canales con enlaces prohibidos.")
    canales = [f"<#{cid}>" for cid in sorted(linkban_canal)]
    embed = discord.Embed(title="🔗 Canales con enlaces prohibidos", description="\n".join(canales), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(link_group)


# ============================================================
#  GRUPO: /log ...
# ============================================================

log_group = app_commands.Group(name="log", description="Gestión de canales de logs")


@log_group.command(name="channel", description="Asigna un canal para enviar logs")
@app_commands.describe(canal="Canal donde enviar los logs")
@app_commands.default_permissions(manage_guild=True)
async def slash_log_channel(interaction: discord.Interaction, canal: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    if canal.id in logs_channels:
        return await interaction.response.send_message(f"ℹ️ {canal.mention} ya estaba configurado como canal de logs.")
    logs_channels.add(canal.id)
    guardar_logs_channels()
    await interaction.response.send_message(f"✅ {canal.mention} configurado como canal de logs.")


@log_group.command(name="unchannel", description="Quita un canal de la lista de logs")
@app_commands.describe(canal="Canal a quitar de logs")
@app_commands.default_permissions(manage_guild=True)
async def slash_log_unchannel(interaction: discord.Interaction, canal: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    if canal.id not in logs_channels:
        return await interaction.response.send_message(f"ℹ️ {canal.mention} no estaba configurado como canal de logs.")
    logs_channels.discard(canal.id)
    guardar_logs_channels()
    await interaction.response.send_message(f"✅ {canal.mention} ya no recibirá logs.")


@log_group.command(name="channels", description="Lista los canales configurados para logs")
@app_commands.default_permissions(manage_guild=True)
async def slash_log_channels(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    if not logs_channels:
        return await interaction.response.send_message("ℹ️ No hay canales de logs configurados. Usa `/log channel #canal` para añadir uno.")
    embed = discord.Embed(title="📋 Canales configurados para logs", color=discord.Color.blurple(), timestamp=datetime.datetime.now(datetime.timezone.utc))
    lineas = []
    for canal_id in sorted(logs_channels):
        canal = interaction.guild.get_channel(canal_id)
        if canal is None:
            lineas.append(f"• `<#{canal_id}>` *(canal ya no existe)*")
        else:
            lineas.append(f"• {canal.mention} (`{canal.id}`)")
    embed.description = "\n".join(lineas)
    embed.set_footer(text=f"Total: {len(logs_channels)} canal(es)")
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(log_group)


# ============================================================
#  SLASH: /purge y /nuke
# ============================================================

@bot.tree.command(name="purge", description="Elimina una cantidad de mensajes del canal actual")
@app_commands.describe(cantidad="Número de mensajes a eliminar (1-1000)")
@app_commands.default_permissions(manage_messages=True)
async def slash_purge(interaction: discord.Interaction, cantidad: int):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Messages.", ephemeral=True)
    if cantidad < 1:
        return await interaction.response.send_message("❌ La cantidad debe ser mayor que 0.", ephemeral=True)
    if cantidad > 1000:
        return await interaction.response.send_message("❌ El máximo permitido es 1000.", ephemeral=True)

    canal = interaction.channel
    # En slash, el propio comando no es un mensaje normal a borrar.
    limite = min(cantidad, 1000)
    restante = limite
    eliminados = 0
    while restante > 0:
        batch = min(restante, 100)
        try:
            borrados = await canal.purge(limit=batch, bulk=True)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ No tengo permisos para borrar.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Error al borrar: {e}", ephemeral=True)
        eliminados += len(borrados)
        restante -= batch
        if len(borrados) < batch:
            break

    await interaction.response.send_message(f"🧹 {eliminados} mensaje(s) eliminado(s) por {interaction.user.mention}.")
    confirmacion = await interaction.original_response()
    try:
        await confirmacion.add_reaction("✅")
    except discord.HTTPException:
        pass


@bot.tree.command(name="nuke", description="Elimina TODOS los mensajes de un canal (lo clona). Si no se especifica, actúa en el canal actual.")
@app_commands.describe(canal="Canal a nukear (opcional; si se omite, se usa el actual)")
@app_commands.default_permissions(manage_messages=True)
async def slash_nuke(interaction: discord.Interaction, canal: discord.TextChannel = None):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Messages.", ephemeral=True)
    if not interaction.guild.me.guild_permissions.manage_channels:
        return await interaction.response.send_message("❌ Necesito el permiso Manage Channels.", ephemeral=True)

    canal = canal or interaction.channel
    try:
        await interaction.response.defer(ephemeral=True, thinking=False)
    except discord.HTTPException:
        pass

    try:
        nuevo = await canal.clone(name=canal.name, reason=f"Nuke por {interaction.user} (ID {interaction.user.id})")
        await nuevo.edit(position=canal.position, category=canal.category, topic=canal.topic, nsfw=canal.nsfw, slowmode_delay=canal.slowmode_delay)
        await canal.delete(reason=f"Nuke por {interaction.user} (ID {interaction.user.id})")
    except discord.Forbidden:
        return await interaction.followup.send("❌ No tengo permisos para clonar/borrar.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.followup.send(f"❌ Error al nukear: {e}", ephemeral=True)

    try:
        await nuevo.send(f"💥 **Nuke ejecutado por {interaction.user.mention}** — canal restablecido.")
    except discord.HTTPException:
        pass


# ============================================================
#  GRUPO: /giveaway ...
# ============================================================

giveaway_group = app_commands.Group(name="giveaway", description="Gestión de sorteos (giveaways)")


@giveaway_group.command(name="create", description="Crea un sorteo con duración y número de ganadores")
@app_commands.describe(nombre="Nombre/título del sorteo", duracion="Duración (ej: 5h, 30m, 1h30m, 10s)", ganadores="Número de ganadores (1-20)")
@app_commands.default_permissions(manage_messages=True)
async def slash_giveaway_create(interaction: discord.Interaction, nombre: str, duracion: str, ganadores: int):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Messages.", ephemeral=True)
    if not nombre or len(nombre) > 100:
        return await interaction.response.send_message("❌ El nombre debe tener entre 1 y 100 caracteres.", ephemeral=True)
    segundos, err = parsear_duracion(duracion)
    if err:
        return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
    if ganadores < 1 or ganadores > 20:
        return await interaction.response.send_message("❌ Indica entre 1 y 20 ganadores.", ephemeral=True)
    if segundos > 86400:
        return await interaction.response.send_message("❌ La duración máxima es de 24 horas.", ephemeral=True)

    gw_id = f"{interaction.guild.id}-{len(giveaways_db) + 1}-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}"
    fin_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=segundos)
    numero_local = len([g for g in giveaways_db.values() if g["guild_id"] == str(interaction.guild.id)]) + 1
    gw = {
        "numero": numero_local,
        "guild_id": str(interaction.guild.id),
        "canal_id": str(interaction.channel.id),
        "mensaje_id": None,
        "nombre": nombre,
        "fin": fin_dt.isoformat(),
        "ganadores_n": ganadores,
        "ganadores": [],
        "finalizado": False,
        "host_id": str(interaction.user.id),
    }

    embed = _embed_giveaway(gw)
    # En slash, hay que responder primero para tener un mensaje en el canal.
    await interaction.response.send_message(embed=embed)
    mensaje = await interaction.original_response()
    try:
        await mensaje.add_reaction("🎉")
    except discord.HTTPException:
        pass

    gw["mensaje_id"] = str(mensaje.id)
    giveaways_db[gw_id] = gw
    guardar_giveaways()

    bot.loop.create_task(_tarea_giveaway(gw_id))


@giveaway_group.command(name="list", description="Muestra todos los sorteos del servidor")
@app_commands.default_permissions(manage_messages=True)
async def slash_giveaway_list(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Messages.", ephemeral=True)
    lista = [(gw_id, gw) for gw_id, gw in giveaways_db.items() if gw["guild_id"] == str(interaction.guild.id)]
    if not lista:
        return await interaction.response.send_message("ℹ️ No hay sorteos en este servidor.")
    embed = discord.Embed(title="🎉 Sorteos del servidor", color=discord.Color.blurple(), timestamp=datetime.datetime.now(datetime.timezone.utc))
    embed.set_footer(text=f"Total: {len(lista)}")
    for gw_id, gw in lista[:25]:
        estado = "✅ Finalizado" if gw.get("finalizado") else "⏳ En curso"
        fin_dt = datetime.datetime.fromisoformat(gw["fin"])
        fin_str = discord.utils.format_dt(fin_dt, "R")
        embed.add_field(name=f"#{gw['numero']} — {gw['nombre']}", value=(f"**Estado:** {estado}\n**Ganadores:** {gw['ganadores_n']}\n**Finaliza:** {fin_str}\n**Host:** <@{gw['host_id']}>"), inline=False)
    await interaction.response.send_message(embed=embed)


@giveaway_group.command(name="delete", description="Elimina un sorteo por su número")
@app_commands.describe(numero="Número del sorteo a eliminar")
@app_commands.default_permissions(manage_messages=True)
async def slash_giveaway_delete(interaction: discord.Interaction, numero: int):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Messages.", ephemeral=True)
    gw_id, gw = _buscar_giveaway_por_numero(interaction.guild.id, numero)
    if gw is None:
        return await interaction.response.send_message(f"❌ No existe un sorteo #{numero} en este servidor.")
    try:
        canal = interaction.guild.get_channel(int(gw["canal_id"]))
        if canal is not None:
            mensaje = await canal.fetch_message(int(gw["mensaje_id"]))
            await mensaje.delete()
    except (discord.HTTPException, AttributeError):
        pass
    del giveaways_db[gw_id]
    guardar_giveaways()
    await interaction.response.send_message(f"✅ Sorteo #{numero} eliminado.")


@giveaway_group.command(name="reroll", description="Elige nuevos ganadores para un sorteo finalizado")
@app_commands.describe(numero="Número del sorteo a hacer reroll")
@app_commands.default_permissions(manage_messages=True)
async def slash_giveaway_reroll(interaction: discord.Interaction, numero: int):
    if not interaction.user.guild_permissions.manage_messages:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Messages.", ephemeral=True)
    gw_id, gw = _buscar_giveaway_por_numero(interaction.guild.id, numero)
    if gw is None:
        return await interaction.response.send_message(f"❌ No existe un sorteo #{numero} en este servidor.")
    if not gw.get("finalizado"):
        return await interaction.response.send_message("❌ Ese sorteo aún no ha finalizado.")
    await interaction.response.defer()
    try:
        canal = interaction.guild.get_channel(int(gw["canal_id"]))
        mensaje = await canal.fetch_message(int(gw["mensaje_id"])) if canal else None
    except (discord.HTTPException, AttributeError):
        mensaje = None
    participantes = []
    if mensaje is not None:
        for reaction in mensaje.reactions:
            if str(reaction.emoji) == "🎉":
                participantes = [u async for u in reaction.users() if not u.bot and u.id != bot.user.id]
                break
    if not participantes:
        return await interaction.followup.send("❌ No había participantes válidos para hacer reroll.")
    random.shuffle(participantes)
    n = gw["ganadores_n"]
    nuevos_ganadores = [p.id for p in participantes[:n]]
    gw["ganadores"] = nuevos_ganadores
    guardar_giveaways()
    embed = _embed_giveaway(gw)
    if mensaje is not None:
        try:
            await mensaje.edit(embed=embed)
        except discord.HTTPException:
            pass
    menciones = " ".join(f"<@{g}>" for g in nuevos_ganadores)
    await interaction.followup.send(f"🎲 Reroll del sorteo #{numero} **{gw['nombre']}**:\n{menciones}")


bot.tree.add_command(giveaway_group)


# ============================================================
#  SLASH: /lock y /unlock
# ============================================================

async def _lock_unlock_slash(interaction, canal, lock: bool):
    if not interaction.user.guild_permissions.manage_channels:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Channels.", ephemeral=True)
    if not interaction.guild.me.guild_permissions.manage_channels:
        return await interaction.response.send_message("❌ Necesito el permiso Manage Channels.", ephemeral=True)
    canal = canal or interaction.channel
    everyone = interaction.guild.default_role
    accion = "lockear" if lock else "unlockear"

    if lock:
        overwrite = canal.overwrites_for(everyone)
        if overwrite.send_messages is False and overwrite.create_public_threads is False:
            return await interaction.response.send_message(f"ℹ️ {canal.mention} ya estaba lockeado.")
        overwrite.send_messages = False
        overwrite.create_public_threads = False
        overwrite.create_private_threads = False
        try:
            await canal.set_permissions(everyone, overwrite=overwrite, reason=f"Lock por {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ No tengo permisos para editar los permisos del canal.")
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Error al lockear: {e}")
    else:
        overwrite = canal.overwrites_for(everyone)
        if overwrite.send_messages is None and overwrite.create_public_threads is None:
            return await interaction.response.send_message(f"ℹ️ {canal.mention} no estaba lockeado.")
        overwrite.send_messages = None
        overwrite.create_public_threads = None
        overwrite.create_private_threads = None
        try:
            await canal.set_permissions(everyone, overwrite=overwrite, reason=f"Unlock por {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ No tengo permisos para editar los permisos del canal.")
        except discord.HTTPException as e:
            return await interaction.response.send_message(f"❌ Error al unlockear: {e}")

    estado = "🔒" if lock else "🔓"
    await interaction.response.send_message(f"{estado} {canal.mention} ha sido {'lockeado' if lock else 'unlockeado'}.")
    embed = discord.Embed(title=("🔒 Canal lockeado" if lock else "🔓 Canal unlockeado"), color=discord.Color.red() if lock else discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Canal", value=canal.mention, inline=True)
    embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
    await enviar_logs(interaction.guild, embed)


@bot.tree.command(name="lock", description="Lockea un canal (no escribir ni abrir hilos)")
@app_commands.describe(canal="Canal a lockear (opcional; si se omite, usa el actual)")
@app_commands.default_permissions(manage_channels=True)
async def slash_lock(interaction: discord.Interaction, canal: discord.TextChannel = None):
    await _lock_unlock_slash(interaction, canal, True)


@bot.tree.command(name="unlock", description="Desbloquea un canal previamente lockeado")
@app_commands.describe(canal="Canal a unlockear (opcional; si se omite, usa el actual)")
@app_commands.default_permissions(manage_channels=True)
async def slash_unlock(interaction: discord.Interaction, canal: discord.TextChannel = None):
    await _lock_unlock_slash(interaction, canal, False)


# ============================================================
#  SLASH: /rename y /namereset
# ============================================================

@bot.tree.command(name="rename", description="Cambia el apodo de un miembro")
@app_commands.describe(miembro="Usuario (mención o nombre)", apodo="Nuevo apodo (máx 32 caracteres)")
@app_commands.default_permissions(manage_nicknames=True)
async def slash_rename(interaction: discord.Interaction, miembro: discord.Member, apodo: str):
    if not interaction.user.guild_permissions.manage_nicknames:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Nicknames.", ephemeral=True)
    apodo = apodo.strip()
    if not apodo:
        return await interaction.response.send_message("❌ El apodo no puede estar vacío.", ephemeral=True)
    if len(apodo) > 32:
        return await interaction.response.send_message("❌ El apodo no puede superar los 32 caracteres.", ephemeral=True)
    if miembro.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        return await interaction.response.send_message("❌ No puedes cambiar el apodo de alguien con rol igual o superior al tuyo.", ephemeral=True)
    if interaction.guild.me.top_role <= miembro.top_role:
        return await interaction.response.send_message("❌ Mi rol es inferior al de ese usuario.", ephemeral=True)
    if miembro.id == interaction.guild.owner_id:
        return await interaction.response.send_message("❌ No puedo cambiar el apodo del dueño del servidor.", ephemeral=True)
    try:
        await miembro.edit(nick=apodo, reason=f"Cambio de apodo por {interaction.user}")
    except discord.Forbidden:
        return await interaction.response.send_message("❌ No tengo permisos para cambiar el apodo.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al cambiar el apodo: {e}", ephemeral=True)
    await interaction.response.send_message(f"✅ Apodo de {miembro.mention} cambiado a `{apodo}`.")


@bot.tree.command(name="namereset", description="Restablece el apodo de un miembro al original")
@app_commands.describe(miembro="Usuario (mención o nombre)")
@app_commands.default_permissions(manage_nicknames=True)
async def slash_namereset(interaction: discord.Interaction, miembro: discord.Member):
    if not interaction.user.guild_permissions.manage_nicknames:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Nicknames.", ephemeral=True)
    if miembro.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        return await interaction.response.send_message("❌ No puedes cambiar el apodo de alguien con rol igual o superior al tuyo.", ephemeral=True)
    if interaction.guild.me.top_role <= miembro.top_role:
        return await interaction.response.send_message("❌ Mi rol es inferior al de ese usuario.", ephemeral=True)
    if miembro.id == interaction.guild.owner_id:
        return await interaction.response.send_message("❌ No puedo cambiar el apodo del dueño del servidor.", ephemeral=True)
    if miembro.nick is None:
        return await interaction.response.send_message(f"ℹ️ {miembro.mention} no tiene apodo personalizado.", ephemeral=True)
    try:
        await miembro.edit(nick=None, reason=f"Reset de apodo por {interaction.user}")
    except discord.Forbidden:
        return await interaction.response.send_message("❌ No tengo permisos.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al restablecer el apodo: {e}", ephemeral=True)
    await interaction.response.send_message(f"✅ Apodo de {miembro.mention} restablecido.")


# ============================================================
#  SLASH: /ipban y /ipunban
# ============================================================

@bot.tree.command(name="ipban", description="Banea por IP (ban + veto persistente vía audit log)")
@app_commands.describe(usuario="Usuario a banear (mención, ID o nombre)", motivo="Motivo (opcional)")
@app_commands.default_permissions(ban_members=True)
async def slash_ipban(interaction: discord.Interaction, usuario: discord.User, motivo: str = "No especificado"):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message("❌ No tienes permiso para banear.", ephemeral=True)
    miembro = interaction.guild.get_member(usuario.id)
    if miembro is not None:
        if miembro.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
            return await interaction.response.send_message("❌ No puedes banear a alguien con rol igual o superior al tuyo.", ephemeral=True)
        if interaction.guild.me.top_role <= miembro.top_role:
            return await interaction.response.send_message("❌ Mi rol es inferior al de ese usuario.", ephemeral=True)
    motivo_full = f"[IP-BAN] {interaction.user} (ID {interaction.user.id}): {motivo}"
    try:
        await interaction.guild.ban(usuario, reason=motivo_full, delete_message_days=1)
    except discord.Forbidden:
        return await interaction.response.send_message("❌ No tengo permisos para banear.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al banear: {e}", ephemeral=True)
    embed = discord.Embed(title="🚫 IP-Ban aplicado", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{usuario} (`{usuario.id}`)", inline=False)
    embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.set_thumbnail(url=usuario.display_avatar.url)
    await interaction.response.send_message(embed=embed)
    await enviar_logs(interaction.guild, embed)


@bot.tree.command(name="ipunban", description="Levanta el IP-ban (desbanea al usuario)")
@app_commands.describe(usuario="Usuario a desbanear (ID o mención)")
@app_commands.default_permissions(ban_members=True)
async def slash_ipunban(interaction: discord.Interaction, usuario: discord.User):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message("❌ No tienes permiso.", ephemeral=True)
    try:
        await interaction.guild.unban(usuario, reason=f"IP-UNBAN por {interaction.user}")
    except discord.NotFound:
        return await interaction.response.send_message("ℹ️ Ese usuario no estaba baneado.", ephemeral=True)
    except discord.Forbidden:
        return await interaction.response.send_message("❌ No tengo permisos para desbanear.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al desbanear: {e}", ephemeral=True)
    embed = discord.Embed(title="✅ IP-Unban aplicado", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{usuario} (`{usuario.id}`)", inline=False)
    embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
    await interaction.response.send_message(embed=embed)
    await enviar_logs(interaction.guild, embed)



# ============================================================
#  SLASH: /soft /softban (baneo temporal)
# ============================================================

soft_group = app_commands.Group(name="soft", description="Baneos temporales (softban)")


@soft_group.command(name="ban", description="Banea temporalmente a un usuario (se desbanea solo)")
@app_commands.describe(usuario="Usuario a banear", duracion="Duración (ej: 5h, 30m, 10s, 1h30m)", motivo="Motivo (opcional)")
@app_commands.default_permissions(ban_members=True)
async def slash_soft_ban(interaction: discord.Interaction, usuario: discord.User, duracion: str, motivo: str = "No especificado"):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message("❌ No tienes permiso para banear.", ephemeral=True)
    segundos, err = parsear_duracion(duracion)
    if err:
        return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
    if segundos > 86400 * 7:
        return await interaction.response.send_message("❌ La duración máxima es de 7 días.", ephemeral=True)
    miembro = interaction.guild.get_member(usuario.id)
    if miembro is not None:
        if miembro.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
            return await interaction.response.send_message("❌ No puedes banear a alguien con rol igual o superior al tuyo.", ephemeral=True)
        if interaction.guild.me.top_role <= miembro.top_role:
            return await interaction.response.send_message("❌ Mi rol es inferior al de ese usuario.", ephemeral=True)
    motivo_full = f"[SOFTBAN {duracion}] {interaction.user} (ID {interaction.user.id}): {motivo}"
    try:
        await interaction.guild.ban(usuario, reason=motivo_full, delete_message_days=0)
    except discord.Forbidden:
        return await interaction.response.send_message("❌ No tengo permisos para banear.", ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.response.send_message(f"❌ Error al banear: {e}", ephemeral=True)
    bot.loop.create_task(_tarea_softban_unban(interaction.guild.id, usuario.id, segundos, interaction.user))
    embed = discord.Embed(title="⏱️ Softban aplicado", color=discord.Color.dark_gold(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Usuario", value=f"{usuario} (`{usuario.id}`)", inline=False)
    embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
    embed.add_field(name="Duración", value=duracion, inline=True)
    embed.add_field(name="Motivo", value=motivo, inline=False)
    embed.set_thumbnail(url=usuario.display_avatar.url)
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(soft_group)


# ============================================================
#  SLASH: /remind (recordatorio)
# ============================================================

@bot.tree.command(name="remind", description="Programa un recordatorio (notificación a ti mismo)")
@app_commands.describe(duracion="Duración (ej: 5h, 30m, 10s, 1h30m)", mensaje="Texto del recordatorio", md="¿Notificar por mensaje privado?")
@app_commands.choices(md=[
    app_commands.Choice(name="Sí, por MD", value="si"),
    app_commands.Choice(name="No, en este canal", value="no"),
])
async def slash_remind(interaction: discord.Interaction, duracion: str, mensaje: str, md: app_commands.Choice[str] = None):
    segundos, err = parsear_duracion(duracion)
    if err:
        return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
    if segundos > 86400 * 30:
        return await interaction.response.send_message("❌ La duración máxima es de 30 días.", ephemeral=True)
    if len(mensaje) > 1000:
        return await interaction.response.send_message("❌ El mensaje no puede superar 1000 caracteres.", ephemeral=True)
    notify_md = bool(md and md.value == "si")
    rid = f"{interaction.user.id}-{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}"
    fin_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=segundos)
    reminders_db[rid] = {
        "user_id": str(interaction.user.id),
        "guild_id": str(interaction.guild.id) if interaction.guild else None,
        "channel_id": str(interaction.channel.id),
        "msg": mensaje,
        "fin": fin_dt.isoformat(),
        "md": notify_md,
    }
    guardar_reminders()
    bot.loop.create_task(_tarea_reminder(rid))
    embed = discord.Embed(title="✅ Recordatorio programado", color=discord.Color.green())
    embed.add_field(name="Para dentro de", value=duracion, inline=True)
    embed.add_field(name="Notificación", value=("MD privado" if notify_md else "En este canal"), inline=True)
    embed.add_field(name="Mensaje", value=mensaje[:1024], inline=False)
    embed.set_footer(text=f"Se te avisará a las {discord.utils.format_dt(fin_dt, 'T')}")
    await interaction.response.send_message(embed=embed)


# ============================================================
#  SLASH: /help /prefix /setprefix /prefixremove
# ============================================================

@bot.tree.command(name="help", description="Muestra la lista de comandos disponibles")
async def slash_help(interaction: discord.Interaction):
    prefijo = get_prefix_message(interaction.guild)
    embed = discord.Embed(
        title="📖 Lista de comandos",
        description=f"Prefijo actual: {prefijo}\nTambién puedes usar slash commands `/` y mencionar al bot.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="🛡️ Moderación", value="`ban` `kick` `unban` `mute` `unmute` `softban`/`soft ban` `ipban` `ipunban`\n`purge` `nuke` `lock` `unlock` `rename` `namereset` `warn`/`warn add` `warnremove`/`warn remove` `warns`/`warn list`", inline=False)
    embed.add_field(name="👥 Roles", value="`roleadd`/`role add` `roleremove`/`role remove` `rolehuman`/`role human` `roleall`/`role all` `rolebot`/`role bot`", inline=False)
    embed.add_field(name="🎉 Sorteos y utilidades", value="`gcreate`/`giveaway create` `glist`/`giveaway list` `gdelete`/`giveaway delete` `greroll`/`giveaway reroll` `avatar` `banner` `remindme`/`remind`", inline=False)
    embed.add_field(name="🔗 Canales y links", value="`linkban`/`link ban` `linkunban`/`link unban` `linkbanlist`/`link list` `logchannel`/`log channel` `logunchannel`/`log unchannel` `logschannels`/`log channels`", inline=False)
    embed.add_field(name="⚙️ Configuración", value=f"`setprefix`/`/setprefix` `prefix` `prefixremove` `sync` `help`", inline=False)
    embed.set_footer(text="Todos funcionan con el prefix indicado y con slash commands.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="prefix", description="Muestra los prefijos activos en el servidor")
async def slash_prefix(interaction: discord.Interaction):
    prefs = _get_prefixes_sync(interaction.guild.id) if interaction.guild else [DEFAULT_PREFIX]
    embed = discord.Embed(
        title="⚙️ Prefijos activos",
        description="\n".join(f"• `{p}`" for p in prefs),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="El prefijo por defecto (.) no se puede eliminar.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="setprefix", description="Añade un prefijo personalizado al servidor")
@app_commands.describe(prefijo="Carácter(es) del nuevo prefijo (máx 5)")
@app_commands.default_permissions(manage_guild=True)
async def slash_setprefix(interaction: discord.Interaction, prefijo: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    prefijo = prefijo.strip()
    if not prefijo:
        return await interaction.response.send_message("❌ El prefijo no puede estar vacío.", ephemeral=True)
    if len(prefijo) > 5:
        return await interaction.response.send_message("❌ El prefijo no puede tener más de 5 caracteres.", ephemeral=True)
    gid = str(interaction.guild.id)
    customs = prefixes_db.setdefault(gid, [])
    if prefijo in customs or prefijo == DEFAULT_PREFIX:
        return await interaction.response.send_message(f"ℹ️ El prefijo `{prefijo}` ya estaba activo.")
    customs.append(prefijo)
    guardar_prefixes()
    await interaction.response.send_message(f"✅ Prefijo `{prefijo}` añadido. Ahora puedes usar `{prefijo}comando`.")


@bot.tree.command(name="prefixremove", description="Elimina un prefijo personalizado del servidor")
@app_commands.describe(prefijo="Prefijo a eliminar")
@app_commands.default_permissions(manage_guild=True)
async def slash_prefixremove(interaction: discord.Interaction, prefijo: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    prefijo = prefijo.strip()
    gid = str(interaction.guild.id)
    customs = prefixes_db.get(gid, [])
    if prefijo == DEFAULT_PREFIX:
        return await interaction.response.send_message("❌ El prefijo por defecto `.` no se puede eliminar.")
    if prefijo not in customs:
        return await interaction.response.send_message(f"ℹ️ El prefijo `{prefijo}` no está configurado en este servidor.")
    customs.remove(prefijo)
    if not customs:
        del prefixes_db[gid]
    guardar_prefixes()
    await interaction.response.send_message(f"✅ Prefijo `{prefijo}` eliminado.")





# ============================================================
#  SLASH: /honeypot
# ============================================================

honeypot_group = app_commands.Group(name="honeypot", description="Configuración de canales honeypot")


@honeypot_group.command(name="set", description="Configura un canal como honeypot (por defecto banea)")
@app_commands.describe(canal="Canal a convertir en honeypot")
@app_commands.default_permissions(manage_guild=True)
async def slash_honeypot_set(interaction: discord.Interaction, canal: discord.TextChannel):
    gid = str(interaction.guild.id)
    cid = str(canal.id)
    honeypots_db.setdefault(gid, {})
    if cid in honeypots_db[gid]:
        return await interaction.response.send_message(f"ℹ️ {canal.mention} ya es un honeypot.", ephemeral=True)
    honeypots_db[gid][cid] = {"action": "ban", "duration": None}
    guardar_honeypots()
    await interaction.response.send_message(f"✅ {canal.mention} configurado como honeypot (acción: ban).")


@honeypot_group.command(name="list", description="Lista los honeypots configurados")
@app_commands.default_permissions(manage_guild=True)
async def slash_honeypot_list(interaction: discord.Interaction):
    gid = str(interaction.guild.id)
    data = honeypots_db.get(gid, {})
    if not data:
        return await interaction.response.send_message("ℹ️ No hay honeypots configurados en este servidor.", ephemeral=True)
    embed = discord.Embed(title="🍯 Honeypots configurados", color=discord.Color.dark_gold())
    lines = []
    for cid_str, cfg in data.items():
        canal = interaction.guild.get_channel(int(cid_str))
        nombre = canal.mention if canal else f"ID {cid_str}"
        lines.append(f"{nombre} → acción: **{cfg['action']}**{' ('+str(cfg['duration'])+'s)' if cfg.get('duration') else ''}")
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@honeypot_group.command(name="remove", description="Elimina un honeypot")
@app_commands.describe(canal="Canal a quitar como honeypot")
@app_commands.default_permissions(manage_guild=True)
async def slash_honeypot_remove(interaction: discord.Interaction, canal: discord.TextChannel):
    gid = str(interaction.guild.id)
    cid = str(canal.id)
    if gid not in honeypots_db or cid not in honeypots_db[gid]:
        return await interaction.response.send_message("❌ Ese canal no es un honeypot.", ephemeral=True)
    del honeypots_db[gid][cid]
    if not honeypots_db[gid]:
        del honeypots_db[gid]
    guardar_honeypots()
    await interaction.response.send_message(f"✅ Honeypot eliminado para {canal.mention}.", ephemeral=True)


@honeypot_group.command(name="config", description="Configura la acción del honeypot")
@app_commands.describe(canal="Canal honeypot", accion="ban/kick/mute", duracion="Duración si mute (ej: 5m, 1h)")
@app_commands.default_permissions(manage_guild=True)
async def slash_honeypot_config(interaction: discord.Interaction, canal: discord.TextChannel, accion: str, duracion: str = None):
    gid = str(interaction.guild.id)
    cid = str(canal.id)
    if gid not in honeypots_db or cid not in honeypots_db[gid]:
        return await interaction.response.send_message("❌ Ese canal no es un honeypot.", ephemeral=True)
    accion = accion.lower()
    if accion not in ("ban", "kick", "mute"):
        return await interaction.response.send_message("❌ Acción inválida. Usa ban, kick o mute.", ephemeral=True)
    cfg = {"action": accion, "duration": None}
    if accion == "mute":
        if not duracion:
            return await interaction.response.send_message("❌ Para mute debes indicar duración (ej: 5m, 1h).", ephemeral=True)
        segundos, err = parsear_duracion(duracion)
        if err:
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        cfg["duration"] = segundos
    honeypots_db[gid][cid] = cfg
    guardar_honeypots()
    await interaction.response.send_message(f"✅ Honeypot {canal.mention} actualizado: acción **{accion}**{' ('+duracion+')' if duracion else ''}.", ephemeral=True)


bot.tree.add_command(honeypot_group)


if __name__ == "__main__":
    import os
    # Buscar token.txt SIEMPRE junto al script, sin importar el directorio de trabajo.
    _dir_script = os.path.dirname(os.path.abspath(__file__))
    _token_path = os.path.join(_dir_script, "token.txt")
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if not TOKEN and os.path.exists(_token_path):
        with open(_token_path, "r", encoding="utf-8") as _f:
            TOKEN = _f.read().strip()
    if not TOKEN:
        print("No se encontró token. Crea un archivo 'token.txt' junto a bot.py con tu token,")
        print(f"o define la variable de entorno DISCORD_TOKEN.")
        print(f"Ruta buscada: {_token_path}")
    else:
        bot.run(TOKEN)
