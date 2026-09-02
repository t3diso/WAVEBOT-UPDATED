import asyncio
import datetime
import json
import os
import random
import re
import time
import sys
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web as dash_web

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

XP_PATH = "xp_data.json"
xp_db = {}  # guild_id (str) -> {user_id (str): {"xp": int, "level": int, "last_xp_gain": float}}
xp_config_db = {}  # guild_id (str) -> {"enabled": bool, "xp_min": int, "xp_max": int, "cooldown": int, "levelup_channel": int|None, "levelup_msg": str|None, "levelup_enabled": bool}

XP_COOLDOWNS = {}  # guild_id (str) -> {user_id (str): timestamp}

# Level role rewards: guild_id -> {level: role_id}
level_roles_db = {}  # guild_id (str) -> {level (str): role_id (str)}
LEVEL_ROLES_PATH = "level_roles.json"

# Autoroles: guild_id -> {"human": [role_id,...], "bot": [role_id,...], "all": [role_id,...]}
autoroles_db = {}  # guild_id (str) -> {"human": [str], "bot": [str], "all": [str]}
AUTOROLES_PATH = "autoroles.json"

PREFIXES_PATH = "prefixes.json"
prefixes_db = {}     # guild_id (str) -> list[str] de prefijos válidos
REMINDERS_PATH = "reminders.json"
reminders_db = {}    # id -> {user_id, channel_id, msg_id, msg, fin, md}

# Starboard
STARBOARD_PATH = "starboard.json"
starboard_db = {}   # guild_id (str) -> {"enabled": bool, "channel_id": int|None, "threshold": int, "posted": {msg_id: star_msg_id}}

# Antiraid (DESACTIVADO por defecto en cada servidor)
ANTIRAID_PATH = "antiraid.json"
antiraid_db = {}    # guild_id (str) -> config (ver _antiraid_default)
# Joins recientes en memoria: guild_id (str) -> [timestamps]
ANTIRAID_JOINS = {}

# Dashboard web (aiohttp, corre junto al bot; se configura en dashboard.json)
DASHBOARD_CONFIG_PATH = "dashboard.json"
DASHBOARD_HTML_PATH = "dashboard.html"
dashboard_config = {"enabled": True, "host": "127.0.0.1", "port": 8080, "token": ""}
_dashboard_arrancado = False
_INICIO_BOT = time.time()

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


async def resolver_objetivo_replica(ctx):
    """
    Si el mensaje del comando es una respuesta (reply) a otro mensaje, devuelve
    (usuario, miembro) del autor de ese mensaje. Si no es una respuesta o no se
    puede resolver, devuelve (None, None).
    """
    ref = ctx.message.reference
    if ref is None:
        return None, None

    resuelto = ref.resolved
    if resuelto is None or isinstance(resuelto, discord.DeletedReferencedMessage):
        try:
            resuelto = await ctx.channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.HTTPException):
            return None, None

    autor = resuelto.author
    if autor is None:
        return None, None

    miembro = ctx.guild.get_member(autor.id) if ctx.guild is not None else None
    return autor, miembro


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


def cargar_level_roles():
    global level_roles_db
    if os.path.exists(LEVEL_ROLES_PATH):
        try:
            with open(LEVEL_ROLES_PATH, "r", encoding="utf-8") as f:
                level_roles_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            level_roles_db = {}
    else:
        level_roles_db = {}
    print(f"Level roles cargados: {sum(len(v) for v in level_roles_db.values())} en {len(level_roles_db)} servidores.")


def guardar_level_roles():
    try:
        with open(LEVEL_ROLES_PATH, "w", encoding="utf-8") as f:
            json.dump(level_roles_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando level_roles.json: {e}")


def cargar_autoroles():
    global autoroles_db
    if os.path.exists(AUTOROLES_PATH):
        try:
            with open(AUTOROLES_PATH, "r", encoding="utf-8") as f:
                autoroles_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            autoroles_db = {}
    else:
        autoroles_db = {}
    total = sum(len(cfg.get("human", [])) + len(cfg.get("bot", [])) + len(cfg.get("all", [])) for cfg in autoroles_db.values())
    print(f"Autoroles cargados: {total} en {len(autoroles_db)} servidores.")


def guardar_autoroles():
    try:
        with open(AUTOROLES_PATH, "w", encoding="utf-8") as f:
            json.dump(autoroles_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando autoroles.json: {e}")


def _autorole_check_permisos(autor: discord.Member, guild: discord.Guild, rol: discord.Role):
    """Valida que el autor y el bot puedan gestionar el rol como autorol. Devuelve un mensaje de error o None."""
    if not autor.guild_permissions.manage_roles:
        return "❌ No tienes permiso para gestionar roles (Manage Roles)."
    if rol.is_default():
        return "❌ No puedes usar el rol `@everyone` como autorol."
    if rol.position >= autor.top_role.position and autor != guild.owner:
        return "❌ No puedes gestionar un autorol igual o superior a tu rol más alto."
    if guild.me.top_role.position <= rol.position:
        return "❌ Ese rol está por encima de mi rol más alto, no puedo gestionarlo."
    if rol.managed:
        return "❌ Ese rol está gestionado por una integración/bot y no se puede usar como autorol."
    return None


def _toggle_autorole(guild: discord.Guild, rol: discord.Role, categoria: str):
    """Añade el rol a la categoría de autoroles si no estaba, o lo quita si ya estaba. Devuelve 'añadido' o 'quitado'."""
    gid = str(guild.id)
    config = autoroles_db.setdefault(gid, {"human": [], "bot": [], "all": []})
    config.setdefault(categoria, [])
    rid = str(rol.id)
    if rid in config[categoria]:
        config[categoria].remove(rid)
        accion = "quitado"
    else:
        config[categoria].append(rid)
        accion = "añadido"
    if not config.get("human") and not config.get("bot") and not config.get("all"):
        del autoroles_db[gid]
    guardar_autoroles()
    return accion


def _construir_embed_autorolelist(guild: discord.Guild) -> discord.Embed:
    gid = str(guild.id)
    config = autoroles_db.get(gid, {})

    def _lineas(categoria):
        ids = config.get(categoria, [])
        if not ids:
            return "*Ninguno.*"
        lineas = []
        for rid in ids:
            rol = guild.get_role(int(rid))
            if rol is None:
                lineas.append(f"• `{rid}` *(rol ya no existe)*")
            else:
                lineas.append(f"• {rol.mention}")
        return "\n".join(lineas)

    embed = discord.Embed(
        title="📋 Autoroles configurados",
        description="Roles que se asignan automáticamente al unirse alguien al servidor.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="🙋 Humanos", value=_lineas("human"), inline=False)
    embed.add_field(name="🤖 Bots", value=_lineas("bot"), inline=False)
    embed.add_field(name="🌐 Generales (todos)", value=_lineas("all"), inline=False)
    total = sum(len(config.get(c, [])) for c in ("human", "bot", "all"))
    embed.set_footer(text=f"Total: {total} autorol(es) configurado(s)")
    return embed


async def check_level_roles(guild: discord.Guild, member: discord.Member, new_level: int):
    """Verifica y asigna roles de recompensa por nivel."""
    gid = str(guild.id)
    if gid not in level_roles_db:
        return
    roles_config = level_roles_db[gid]
    for level_str, role_id_str in roles_config.items():
        level_req = int(level_str)
        if new_level >= level_req:
            role = guild.get_role(int(role_id_str))
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Recompensa por alcanzar nivel {level_req}")
                except discord.HTTPException:
                    pass


def cargar_level_roles():
    global level_roles_db
    if os.path.exists(LEVEL_ROLES_PATH):
        try:
            with open(LEVEL_ROLES_PATH, "r", encoding="utf-8") as f:
                level_roles_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            level_roles_db = {}
    else:
        level_roles_db = {}
    print(f"Level roles cargados: {sum(len(v) for v in level_roles_db.values())} en {len(level_roles_db)} servidores.")


def guardar_level_roles():
    try:
        with open(LEVEL_ROLES_PATH, "w", encoding="utf-8") as f:
            json.dump(level_roles_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando level_roles.json: {e}")
    global xp_db, xp_config_db
    if os.path.exists(XP_PATH):
        try:
            with open(XP_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            xp_db = data.get("xp", {})
            xp_config_db = data.get("config", {})
        except (json.JSONDecodeError, OSError):
            xp_db = {}
            xp_config_db = {}
    else:
        xp_db = {}
        xp_config_db = {}
    print(f"XP data cargado: {sum(len(v) for v in xp_db.values())} usuarios en {len(xp_db)} servidores.")


def cargar_xp():
    global xp_db, xp_config_db
    if os.path.exists(XP_PATH):
        try:
            with open(XP_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            xp_db = data.get("xp", {})
            xp_config_db = data.get("config", {})
        except (json.JSONDecodeError, OSError):
            xp_db = {}
            xp_config_db = {}
    else:
        xp_db = {}
        xp_config_db = {}
    print(f"XP data cargado: {sum(len(v) for v in xp_db.values())} usuarios en {len(xp_db)} servidores.")


def guardar_xp():
    try:
        with open(XP_PATH, "w", encoding="utf-8") as f:
            json.dump({"xp": xp_db, "config": xp_config_db}, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando xp_data.json: {e}")


def get_xp_config(guild_id: int) -> dict:
    """Obtiene la configuración XP para un servidor, con valores por defecto."""
    gid = str(guild_id)
    default = {
        "enabled": False,
        "xp_min": 15,
        "xp_max": 25,
        "cooldown": 60,
        "levelup_channel": None,
        "levelup_msg": "🎉 ¡{user} ha subido al nivel {level}! (XP total: {xp})",
        "levelup_enabled": True,
    }
    if gid not in xp_config_db:
        xp_config_db[gid] = default.copy()
    else:
        # Asegurar que existan todas las claves
        for k, v in default.items():
            xp_config_db[gid].setdefault(k, v)
    return xp_config_db[gid]


def get_user_xp(guild_id: int, user_id: int) -> dict:
    """Obtiene los datos de XP de un usuario, inicializando si no existen."""
    gid = str(guild_id)
    uid = str(user_id)
    if gid not in xp_db:
        xp_db[gid] = {}
    if uid not in xp_db[gid]:
        xp_db[gid][uid] = {"xp": 0, "level": 0, "last_xp_gain": 0}
    return xp_db[gid][uid]


def xp_for_level(level: int) -> int:
    """Calcula el XP total requerido para alcanzar un nivel."""
    # Fórmula: 5 * level^2 + 50 * level  (nivel 0 = 0 XP)
    return 5 * level * level + 50 * level


def level_from_xp(xp: int) -> int:
    """Calcula el nivel basado en el XP total."""
    level = 0
    while xp >= xp_for_level(level + 1):
        level += 1
    return level


def get_xp_progress(user_data: dict) -> tuple:
    """Retorna (xp_actual, xp_para_siguiente_nivel, porcentaje_progreso)."""
    xp = user_data["xp"]
    level = user_data["level"]
    xp_current_level = xp_for_level(level)
    xp_next_level = xp_for_level(level + 1)
    xp_in_level = xp - xp_current_level
    xp_needed = xp_next_level - xp_current_level
    progress = (xp_in_level / xp_needed) * 100 if xp_needed > 0 else 100
    return xp_in_level, xp_needed, progress


def get_guild_leaderboard(guild_id: int) -> list:
    """Retorna lista ordenada de (user_id, xp, level) para el servidor."""
    gid = str(guild_id)
    if gid not in xp_db:
        return []
    users = []
    for uid, data in xp_db[gid].items():
        users.append((int(uid), data["xp"], data["level"]))
    users.sort(key=lambda x: (-x[1], -x[2]))
    return users


def get_user_rank(guild_id: int, user_id: int) -> int:
    """Obtiene la posición en el ranking de un usuario (1-indexed)."""
    leaderboard = get_guild_leaderboard(guild_id)
    for i, (uid, _, _) in enumerate(leaderboard, 1):
        if uid == user_id:
            return i
    return 0


def create_progress_bar(progress: float, length: int = 10) -> str:
    """Crea una barra de progreso visual."""
    filled = int(length * progress / 100)
    return "█" * filled + "░" * (length - filled)
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


def cargar_starboard():
    global starboard_db
    if os.path.exists(STARBOARD_PATH):
        try:
            with open(STARBOARD_PATH, "r", encoding="utf-8") as f:
                starboard_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            starboard_db = {}
    else:
        starboard_db = {}
    print(f"Starboard configs cargados: {len(starboard_db)} servidores.")


def guardar_starboard():
    try:
        with open(STARBOARD_PATH, "w", encoding="utf-8") as f:
            json.dump(starboard_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando starboard.json: {e}")


def cargar_antiraid():
    global antiraid_db
    if os.path.exists(ANTIRAID_PATH):
        try:
            with open(ANTIRAID_PATH, "r", encoding="utf-8") as f:
                antiraid_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            antiraid_db = {}
    else:
        antiraid_db = {}
    # Rellenar claves que falten en configs guardadas (compatibilidad).
    for cfg in antiraid_db.values():
        base = _antiraid_default()
        for clave, valor in base.items():
            cfg.setdefault(clave, valor)
        cfg["stats"].setdefault("raids", 0)
        cfg["stats"].setdefault("punished", 0)
    print(f"Antiraid configs cargados: {len(antiraid_db)} servidores.")


def guardar_antiraid():
    try:
        with open(ANTIRAID_PATH, "w", encoding="utf-8") as f:
            json.dump(antiraid_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando antiraid.json: {e}")


def cargar_dashboard():
    """Carga dashboard.json (opcional): enabled, host, port, token."""
    global dashboard_config
    if os.path.exists(DASHBOARD_CONFIG_PATH):
        try:
            with open(DASHBOARD_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                dashboard_config.update(data)
        except (json.JSONDecodeError, OSError):
            pass
    dashboard_config["enabled"] = bool(dashboard_config.get("enabled", True))
    dashboard_config["host"] = str(dashboard_config.get("host", "127.0.0.1"))
    try:
        dashboard_config["port"] = int(dashboard_config.get("port", 8080))
    except (TypeError, ValueError):
        dashboard_config["port"] = 8080
    dashboard_config["token"] = str(dashboard_config.get("token", ""))
    dashboard_config["owner_id"] = str(dashboard_config.get("owner_id", "")).strip()
    estado = "activado" if dashboard_config["enabled"] else "desactivado"
    print(f"Dashboard: {estado} -> http://{dashboard_config['host']}:{dashboard_config['port']}")


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

    # XP System
    config = get_xp_config(message.guild.id)
    if config["enabled"]:
        user_data = get_user_xp(message.guild.id, message.author.id)
        now = time.time()
        last_gain = user_data.get("last_xp_gain", 0)
        if now - last_gain >= config["cooldown"]:
            xp_gain = random.randint(config["xp_min"], config["xp_max"])
            old_level = user_data["level"]
            user_data["xp"] += xp_gain
            user_data["last_xp_gain"] = now
            new_level = level_from_xp(user_data["xp"])
            user_data["level"] = new_level
            if new_level > old_level:
                # Level up!
                if config["levelup_enabled"]:
                    msg = config["levelup_msg"]
                    msg = msg.replace("{user}", message.author.mention)
                    msg = msg.replace("{level}", str(new_level))
                    msg = msg.replace("{xp}", str(user_data["xp"]))
                    msg = msg.replace("{server}", message.guild.name)
                    channel_id = config["levelup_channel"]
                    if channel_id:
                        channel = message.guild.get_channel(channel_id)
                    else:
                        channel = message.channel
                    if channel:
                        embed = discord.Embed(
                            title="🎉 ¡Subida de nivel!",
                            description=msg,
                            color=discord.Color.gold(),
                            timestamp=discord.utils.utcnow()
                        )
                        embed.set_thumbnail(url=message.author.display_avatar.url)
                        embed.add_field(name="Nuevo nivel", value=str(new_level), inline=True)
                        embed.add_field(name="XP total", value=str(user_data["xp"]), inline=True)
                        try:
                            await channel.send(embed=embed)
                        except discord.HTTPException:
                            pass
                # Check level role rewards
                await check_level_roles(message.guild, message.author, new_level)
            guardar_xp()

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
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Starboard handling for ⭐
    if payload.emoji.name != "⭐":
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    config = starboard_db.get(str(guild.id))
    if not config or not config.get("enabled"):
        return
    channel_id = config.get("channel_id")
    threshold = config.get("threshold", 5)
    if not channel_id:
        return
    star_channel = guild.get_channel(channel_id)
    if not star_channel:
        return
    # Ignore reactions in starboard channel itself
    if payload.channel_id == channel_id:
        return
    # Fetch message
    channel = guild.get_channel(payload.channel_id)
    if not channel:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return
    except discord.HTTPException:
        return
    # Count ⭐ reactions
    star_reaction = None
    for reaction in message.reactions:
        if str(reaction.emoji) == "⭐":
            star_reaction = reaction
            break
    if not star_reaction:
        return
    if star_reaction.count < threshold:
        return
    # Check if already posted
    posted = config.get("posted", {})
    if str(message.id) in posted:
        return
    # Create embed for starboard
    embed = discord.Embed(
        description=message.content[:4096] if message.content else "*(sin texto)*",
        color=message.author.color or discord.Color.gold(),
        timestamp=message.created_at,
    )
    embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    embed.add_field(name="Canal", value=message.channel.mention, inline=True)
    embed.add_field(name="⭐", value=str(star_reaction.count), inline=True)
    embed.set_footer(text=f"ID: {message.id}")
    if message.attachments:
        embed.set_image(url=message.attachments[0].url)
    try:
        star_msg = await star_channel.send(embed=embed)
        posted[str(message.id)] = star_msg.id
        config["posted"] = posted
        guardar_starboard()
    except discord.HTTPException:
        pass


@bot.event
async def on_ready():
    cargar_linkban()
    cargar_warns()
    cargar_logs_channels()
    cargar_giveaways()
    cargar_prefixes()
    cargar_reminders()
    cargar_honeypots()
    cargar_xp()
    cargar_level_roles()
    cargar_autoroles()
    cargar_starboard()
    cargar_economy()
    cargar_shop()
    cargar_antiraid()
    cargar_dashboard()
    # Arrancar el dashboard web (una sola vez, aunque on_ready se repita al reconectar).
    global _dashboard_arrancado
    if dashboard_config["enabled"] and not _dashboard_arrancado:
        _dashboard_arrancado = True
        try:
            await _iniciar_dashboard()
        except OSError as e:
            print(f"Error arrancando el dashboard: {e}")
    await bot.change_presence(status=discord.Status.online, activity=discord.Game(name=".help | created by fakepy"))
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
async def ban(ctx, *, args: str = ""):
    """
    Banea a un usuario por ID, @mención, nombre, o respondiendo a su mensaje.
    Uso: .ban <id|@|nombre> [motivo]
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .ban [motivo]
    """
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario, miembro = usuario_repl, miembro_repl
        motivo = args.strip() or "No especificado"
    else:
        tokens = args.split(maxsplit=1)
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.ban <id|@|nombre> [motivo]`"
            )
        usuario_arg = tokens[0]
        motivo = tokens[1] if len(tokens) > 1 else "No especificado"
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
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me faltan permisos (Ban members) para ejecutar este comando.")


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx, *, args: str = ""):
    """
    Expulsa a un miembro por ID, @mención, nombre, o respondiendo a su mensaje.
    Uso: .kick <id|@|nombre> [motivo]
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .kick [motivo]
    """
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario, miembro = usuario_repl, miembro_repl
        motivo = args.strip() or "No especificado"
    else:
        tokens = args.split(maxsplit=1)
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.kick <id|@|nombre> [motivo]`"
            )
        usuario_arg = tokens[0]
        motivo = tokens[1] if len(tokens) > 1 else "No especificado"
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
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me faltan permisos (Kick members) para ejecutar este comando.")


DURACION_MUTE_DEFECTO = "5m"


@bot.command(name="mute")
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def mute(ctx, *, args: str = ""):
    """
    Silencia (timeout) a un miembro por ID, @mención, nombre, o respondiendo a su mensaje.
    Uso: .mute [id|@|nombre] [duración] [motivo]
    Si respondes al mensaje del usuario, no hace falta indicar el usuario.
    Si no indicas duración, se usan 5 minutos por defecto.
    La duración usa h/m/s. Ejemplos: 5h, 30m, 10s, 1h30m, 2h15m30s.
    """
    tokens = args.split()

    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario, miembro = usuario_repl, miembro_repl
    else:
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.mute [usuario] [duración] [motivo]`"
            )
        usuario_arg = tokens.pop(0)
        usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
        if err:
            return await ctx.send(err)

    duracion = DURACION_MUTE_DEFECTO
    if tokens:
        _, err_duracion = parsear_duracion(tokens[0])
        if err_duracion is None:
            duracion = tokens.pop(0)

    motivo = " ".join(tokens).strip() or "No especificado"

    segundos, err = parsear_duracion(duracion)
    if err:
        return await ctx.send(f"❌ {err}")

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
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me faltan permisos (Timeout members) para ejecutar este comando.")


@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def unmute(ctx, *, args: str = ""):
    """
    Quita el timeout a un miembro por ID, @mención, nombre, o respondiendo a su mensaje.
    Uso: .unmute <id|@|nombre> [motivo]
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .unmute [motivo]
    """
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario, miembro = usuario_repl, miembro_repl
        motivo = args.strip() or "No especificado"
    else:
        tokens = args.split(maxsplit=1)
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.unmute <id|@|nombre> [motivo]`"
            )
        usuario_arg = tokens[0]
        motivo = tokens[1] if len(tokens) > 1 else "No especificado"
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
async def unban(ctx, *, args: str = ""):
    """
    Levanta el ban de un usuario por ID, @mención, nombre, o respondiendo a su mensaje.
    Uso: .unban <id|@|nombre> [motivo]
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .unban [motivo]
    """
    usuario_repl, _ = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario = usuario_repl
        motivo = args.strip() or "No especificado"
    else:
        tokens = args.split(maxsplit=1)
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.unban <id|@|nombre> [motivo]`"
            )
        usuario_arg = tokens[0]
        motivo = tokens[1] if len(tokens) > 1 else "No especificado"
        # Nota: unban por nombre requiere que la caché del bot tenga el usuario; lo más fiable es ID/@.
        usuario, _, err = await resolver_usuario(ctx.guild, usuario_arg)
        if err:
            # Si vino un nombre y no está en caché, intentamos buscar en la lista de baneados.
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
async def warn(ctx, *, args: str = ""):
    """
    Advierte a un usuario. Uso: .warn <id|@|nombre> <motivo> (motivo obligatorio)
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .warn <motivo>
    """
    usuario_repl, _ = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario = usuario_repl
        motivo = args.strip()
        if not motivo:
            return await ctx.send("❌ Debes indicar un motivo. Uso: `.warn <motivo>` (respondiendo al mensaje)")
    else:
        tokens = args.split(maxsplit=1)
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.warn <id|@|nombre> <motivo>`"
            )
        usuario_arg = tokens[0]
        motivo = tokens[1].strip() if len(tokens) > 1 else ""
        if not motivo:
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
async def warnremove(ctx, *, args: str = ""):
    """
    Elimina un warn por su número. Uso: .warnremove <id|@|nombre> <número>
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .warnremove <número>
    """
    usuario_repl, _ = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario = usuario_repl
        resto = args.split()
        if not resto:
            return await ctx.send("❌ Debes indicar el número de warn. Uso: `.warnremove <número>` (respondiendo al mensaje)")
        try:
            numero = int(resto[0])
        except ValueError:
            return await ctx.send("❌ El número debe ser un entero.")
    else:
        tokens = args.split()
        if len(tokens) < 2:
            return await ctx.send(
                "❌ Debes indicar un usuario y un número, o responder al mensaje del usuario.\n"
                "Uso correcto: `.warnremove <id|@|nombre> <número>`"
            )
        usuario_arg, numero_str = tokens[0], tokens[1]
        try:
            numero = int(numero_str)
        except ValueError:
            return await ctx.send("❌ El número debe ser un entero.")
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
async def warns(ctx, *, args: str = ""):
    """
    Muestra todos los warns de un usuario con motivo y fecha. Uso: .warns <id|@|nombre>
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .warns
    """
    usuario_repl, _ = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario = usuario_repl
    else:
        tokens = args.split()
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.warns <id|@|nombre>`"
            )
        usuario, _, err = await resolver_usuario(ctx.guild, tokens[0])
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
async def roleadd(ctx, *, args: str = ""):
    """
    Otorga un rol a un usuario. Uso: .roleadd <id|@|nombre> @rol
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .roleadd @rol
    """
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)
    tokens = args.split()

    if usuario_repl is not None:
        usuario, miembro = usuario_repl, miembro_repl
        if not tokens:
            return await ctx.send("❌ Debes indicar un rol. Uso: `.roleadd @rol` (respondiendo al mensaje)")
        rol_arg = tokens[0]
    else:
        if len(tokens) < 2:
            return await ctx.send(
                "❌ Debes indicar un usuario y un rol, o responder al mensaje del usuario.\n"
                "Uso correcto: `.roleadd <id|@|nombre> @rol`"
            )
        usuario_arg, rol_arg = tokens[0], tokens[1]
        usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
        if err:
            return await ctx.send(err)

    try:
        rol = await commands.RoleConverter().convert(ctx, rol_arg)
    except commands.RoleNotFound:
        return await ctx.send("❌ No encontré ese rol. Menciónalo con @rol, usa su ID o su nombre exacto.")

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
async def roleremove(ctx, *, args: str = ""):
    """
    Quita un rol a un usuario. Uso: .roleremove <id|@|nombre> @rol
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .roleremove @rol
    """
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)
    tokens = args.split()

    if usuario_repl is not None:
        usuario, miembro = usuario_repl, miembro_repl
        if not tokens:
            return await ctx.send("❌ Debes indicar un rol. Uso: `.roleremove @rol` (respondiendo al mensaje)")
        rol_arg = tokens[0]
    else:
        if len(tokens) < 2:
            return await ctx.send(
                "❌ Debes indicar un usuario y un rol, o responder al mensaje del usuario.\n"
                "Uso correcto: `.roleremove <id|@|nombre> @rol`"
            )
        usuario_arg, rol_arg = tokens[0], tokens[1]
        usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
        if err:
            return await ctx.send(err)

    try:
        rol = await commands.RoleConverter().convert(ctx, rol_arg)
    except commands.RoleNotFound:
        return await ctx.send("❌ No encontré ese rol. Menciónalo con @rol, usa su ID o su nombre exacto.")

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


@roleremove.error
async def roleremove_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Roles.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Roles.")


@bot.command(name="avatar")
async def avatar(ctx, *, args: str = ""):
    """
    Muestra el avatar completo de un usuario. Uso: .avatar <id|@|nombre>
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .avatar
    """
    usuario_repl, _ = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario = usuario_repl
    else:
        tokens = args.split()
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.avatar <id|@|nombre>`"
            )
        usuario, _, err = await resolver_usuario(ctx.guild, tokens[0])
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
async def banner(ctx, *, args: str = ""):
    """
    Muestra el banner completo de un usuario. Uso: .banner <id|@|nombre>
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .banner
    """
    usuario_repl, _ = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario = usuario_repl
    else:
        tokens = args.split()
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.banner <id|@|nombre>`"
            )
        usuario, _, err = await resolver_usuario(ctx.guild, tokens[0])
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


@bot.command(name="jumbo")
async def jumbo(ctx, emoji: str):
    """Aumenta un emoji (personalizado o Unicode). Uso: .jumbo <emoji>"""
    # Intentar parsear como emoji personalizado
    custom = discord.PartialEmoji.from_str(emoji)
    if custom.id:
        # Emoji personalizado (animado o no)
        url = custom.url
        name = custom.name
        embed = discord.Embed(title=f"Jumbo: {name}", color=discord.Color.blurple())
        embed.set_image(url=url)
        return await ctx.send(embed=embed)
    # Emoji Unicode: usar twemoji CDN
    # Obtener codepoint
    codepoints = "-".join(f"{ord(c):x}" for c in emoji)
    url = f"https://twemoji.maxcdn.com/v/latest/72x72/{codepoints}.png"
    embed = discord.Embed(title="Jumbo", color=discord.Color.blurple())
    embed.set_image(url=url)
    await ctx.send(embed=embed)


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
#  AUTOROLE (comandos prefix)
# ============================================================

@bot.command(name="autorolehuman")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def autorolehuman(ctx, rol: discord.Role):
    """Añade o quita un autorol para miembros humanos. Uso: .autorolehuman @rol"""
    error = _autorole_check_permisos(ctx.author, ctx.guild, rol)
    if error:
        return await ctx.send(error)
    accion = _toggle_autorole(ctx.guild, rol, "human")
    await ctx.send(f"✅ Rol {rol.mention} **{accion}** como autorol para **miembros humanos**.")


@autorolehuman.error
async def autorolehuman_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Roles.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.autorolehuman @rol`")
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send("❌ No encontré ese rol. Menciónalo con @rol.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Roles.")


@bot.command(name="autorolebot")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def autorolebot(ctx, rol: discord.Role):
    """Añade o quita un autorol para bots. Uso: .autorolebot @rol"""
    error = _autorole_check_permisos(ctx.author, ctx.guild, rol)
    if error:
        return await ctx.send(error)
    accion = _toggle_autorole(ctx.guild, rol, "bot")
    await ctx.send(f"✅ Rol {rol.mention} **{accion}** como autorol para **bots**.")


@autorolebot.error
async def autorolebot_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Roles.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.autorolebot @rol`")
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send("❌ No encontré ese rol. Menciónalo con @rol.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Roles.")


@bot.command(name="autorole")
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def autorole(ctx, rol: discord.Role):
    """Añade o quita un autorol para TODOS los miembros (humanos y bots). Uso: .autorole @rol"""
    error = _autorole_check_permisos(ctx.author, ctx.guild, rol)
    if error:
        return await ctx.send(error)
    accion = _toggle_autorole(ctx.guild, rol, "all")
    await ctx.send(f"✅ Rol {rol.mention} **{accion}** como autorol para **todos los miembros**.")


@autorole.error
async def autorole_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Roles.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Uso correcto: `.autorole @rol`")
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send("❌ No encontré ese rol. Menciónalo con @rol.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Manage Roles.")


@bot.command(name="autorolelist", aliases=["autoroleslist"])
@commands.has_permissions(manage_roles=True)
async def autorolelist(ctx):
    """Lista los autoroles configurados en el servidor, separados por categoría."""
    embed = _construir_embed_autorolelist(ctx.guild)
    await ctx.send(embed=embed)


@autorolelist.error
async def autorolelist_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Roles.")


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
async def rename(ctx, *, args: str = ""):
    """
    Cambia el apodo de un miembro. Uso: .rename <id|@|nombre> <nuevo apodo>
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .rename <apodo>
    """
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario, miembro = usuario_repl, miembro_repl
        apodo = args.strip()
    else:
        tokens = args.split(maxsplit=1)
        if len(tokens) < 2 or not tokens[1].strip():
            return await ctx.send(
                "❌ Debes indicar un usuario y un apodo, o responder al mensaje del usuario.\n"
                "Uso correcto: `.rename <id|@|nombre> <apodo>`"
            )
        usuario_arg, apodo = tokens[0], tokens[1].strip()
        usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
        if err:
            return await ctx.send(err)

    if not apodo:
        return await ctx.send("❌ Debes indicar un apodo. Uso: `.rename <id|@|nombre> <apodo>`")
    if len(apodo) > 32:
        return await ctx.send("❌ El apodo no puede superar los 32 caracteres.")

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
async def namereset(ctx, *, args: str = ""):
    """
    Restablece el apodo de un miembro al original. Uso: .namereset <id|@|nombre>
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .namereset
    """
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario, miembro = usuario_repl, miembro_repl
    else:
        tokens = args.split()
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.namereset <id|@|nombre>`"
            )
        usuario, miembro, err = await resolver_usuario(ctx.guild, tokens[0])
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
async def ipban(ctx, *, args: str = ""):
    """
    Banea por IP equivalente: banea al usuario + veto vía audit log.
    Uso: .ipban <id|@|nombre> [motivo]
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .ipban [motivo]
    """
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario, miembro = usuario_repl, miembro_repl
        motivo = args.strip() or "No especificado"
    else:
        tokens = args.split(maxsplit=1)
        if not tokens:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.ipban <id|@|nombre> [motivo]`"
            )
        usuario_arg = tokens[0]
        motivo = tokens[1] if len(tokens) > 1 else "No especificado"
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
async def ipunban(ctx, *, args: str = ""):
    """
    Levanta el IP-ban (desbanea al usuario) por ID, @mención, nombre, o respondiendo a su mensaje.
    Uso: .ipunban <id|@|nombre>
    Si respondes al mensaje del usuario, no hace falta indicar el usuario: .ipunban
    """
    usuario_repl, _ = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        usuario = usuario_repl
    else:
        usuario_arg = args.strip()
        if not usuario_arg:
            return await ctx.send(
                "❌ Debes indicar un usuario (ID, @mención o nombre) o responder a su mensaje.\n"
                "Uso correcto: `.ipunban <id|@|nombre>`"
            )
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
        await ctx.send("❌ Uso correcto: `.ipunban <id_usuario>` o responde al mensaje del usuario.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ La ID debe ser un número entero, una mención o un nombre válido.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me falta el permiso Ban Members.")


# ============================================================
#  ANTIRAID (detección de joins masivos, DESACTIVADO por defecto)
# ============================================================

ANTIRAID_MUTE_MINUTOS = 60  # duración del timeout cuando la acción es "mute"


def _antiraid_default():
    """Config por defecto del antiraid: desactivado."""
    return {
        "enabled": False,      # sistema desactivado por defecto
        "action": "kick",      # ban | kick | mute
        "threshold": 5,        # joins necesarios dentro de la ventana para considerar raid
        "seconds": 10,         # ventana de tiempo en segundos
        "punish_new": True,    # castigar a quienes entren mientras el modo raid esté activo
        "min_age": 0,          # edad mínima de la cuenta en minutos (0 = desactivado)
        "active": False,       # modo raid activo ahora mismo
        "activated_at": None,  # timestamp de activación
        "manual": False,       # activado manualmente (no se desactiva solo)
        "stats": {"raids": 0, "punished": 0},
    }


def _antiraid_cfg(guild_id):
    """Devuelve (creándola si no existe) la config antiraid de un servidor."""
    gid = str(guild_id)
    cfg = antiraid_db.setdefault(gid, _antiraid_default())
    base = _antiraid_default()
    for clave, valor in base.items():
        cfg.setdefault(clave, valor)
    cfg["stats"].setdefault("raids", 0)
    cfg["stats"].setdefault("punished", 0)
    return cfg


def _antiraid_status_embed(cfg: dict) -> discord.Embed:
    """Embed con el estado/config actual del antiraid (sin footer)."""
    accion = cfg.get("action", "kick")
    embed = discord.Embed(
        title="🚨 Antiraid",
        color=discord.Color.dark_red() if cfg.get("enabled") else discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Estado", value="🟢 Activado" if cfg.get("enabled") else "🔴 Desactivado (por defecto)", inline=True)
    raid_txt = "🚨 ACTIVO (castigando entradas)" if cfg.get("active") else "Inactivo"
    if cfg.get("active") and cfg.get("manual"):
        raid_txt += " • manual"
    embed.add_field(name="Modo raid", value=raid_txt, inline=True)
    embed.add_field(name="Detección", value=f"**{cfg.get('threshold', 5)}** joins en **{cfg.get('seconds', 10)}s**", inline=False)
    accion_txt = f"`{accion}`" + (f" (timeout {ANTIRAID_MUTE_MINUTOS} min)" if accion == "mute" else "")
    embed.add_field(name="Acción", value=accion_txt, inline=True)
    embed.add_field(name="Castigar entradas en raid", value="Sí" if cfg.get("punish_new", True) else "No", inline=True)
    min_age = int(cfg.get("min_age", 0))
    embed.add_field(name="Edad mínima cuenta", value=f"{min_age} min" if min_age > 0 else "Desactivada", inline=True)
    stats = cfg.get("stats", {})
    embed.add_field(name="Stats", value=f"Raids detectados: {stats.get('raids', 0)} • Castigados: {stats.get('punished', 0)}", inline=False)
    return embed


async def _antiraid_punish(member: discord.Member, cfg: dict, motivo: str):
    """Aplica la acción configurada (ban/kick/mute) a un miembro. Devuelve True si se castigó."""
    razon = f"[ANTIRAID] {motivo}"
    accion = cfg.get("action", "kick")
    try:
        if accion == "ban":
            await member.ban(reason=razon, delete_message_days=1)
        elif accion == "mute":
            await member.edit(
                communication_disabled_until=discord.utils.utcnow() + datetime.timedelta(minutes=ANTIRAID_MUTE_MINUTOS),
                reason=razon,
            )
        else:
            await member.kick(reason=razon)
        stats = cfg.setdefault("stats", {})
        stats["punished"] = int(stats.get("punished", 0)) + 1
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def _antiraid_check(member: discord.Member):
    """
    Lógica antiraid al unirse un miembro. Devuelve True si fue castigado
    (en ese caso no se le aplican autoroles).
    """
    gid = str(member.guild.id)
    cfg = antiraid_db.get(gid)
    if not cfg or not cfg.get("enabled"):
        return False

    # Nunca castigar al propio bot, al dueño del servidor ni al staff con Manage Server.
    if member.id == bot.user.id or member.id == member.guild.owner_id or member.guild_permissions.manage_guild:
        return False

    ahora = time.time()
    ventana = max(int(cfg.get("seconds", 10)), 3)

    # Registrar el join SIEMPRE (sirve para detectar el raid y para mantener activo el modo).
    joins = ANTIRAID_JOINS.setdefault(gid, [])
    joins.append(ahora)
    joins[:] = [t for t in joins if ahora - t <= ventana]

    # Modo raid ya activo → castigo directo.
    if cfg.get("active"):
        await _antiraid_punish(member, cfg, "Modo raid activo")
        guardar_antiraid()
        return True

    # Filtro de edad mínima de cuenta.
    if int(cfg.get("min_age", 0)) > 0:
        edad_minutos = (ahora - member.created_at.timestamp()) / 60
        if edad_minutos < int(cfg["min_age"]):
            await _antiraid_punish(member, cfg, f"Cuenta demasiado nueva (< {cfg['min_age']} min)")
            guardar_antiraid()
            return True

    # ¿Umbral de joins alcanzado dentro de la ventana? → raid detectado.
    if len(joins) >= max(int(cfg.get("threshold", 5)), 2):
        cfg["active"] = True
        cfg["activated_at"] = ahora
        cfg["manual"] = False
        stats = cfg.setdefault("stats", {})
        stats["raids"] = int(stats.get("raids", 0)) + 1
        motivo = f"Raid detectado ({len(joins)} joins en {ventana}s)"

        castigados = 0
        if cfg.get("punish_new", True):
            # Castigar a toda la ráfaga: quienes entraron en la ventana (incluye al que la dispara).
            for m in list(member.guild.members):
                if m.id == bot.user.id or m.id == m.guild.owner_id or m.guild_permissions.manage_guild:
                    continue
                if m.joined_at is not None and (ahora - m.joined_at.timestamp()) <= ventana:
                    if await _antiraid_punish(m, cfg, motivo):
                        castigados += 1
        else:
            # Solo castigar al miembro que disparó el umbral.
            if await _antiraid_punish(member, cfg, motivo):
                castigados += 1

        guardar_antiraid()

        embed = discord.Embed(
            title="🚨 RAID DETECTADO",
            description=f"Modo raid activado automáticamente: {len(joins)} joins en {ventana}s.",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Acción", value=str(cfg.get("action", "kick")).upper(), inline=True)
        embed.add_field(name="Castigados", value=str(castigados), inline=True)
        embed.set_footer(text="El modo raid se desactiva solo cuando dejen de entrar usuarios.")
        await enviar_logs(member.guild, embed)

        bot.loop.create_task(_antiraid_auto_off(gid))
        return True

    return False


async def _antiraid_auto_off(gid: str):
    """Desactiva el modo raid automático cuando no entran usuarios durante una ventana completa."""
    while True:
        cfg = antiraid_db.get(gid)
        if not cfg or not cfg.get("active") or cfg.get("manual"):
            return
        ventana = max(int(cfg.get("seconds", 10)), 3)
        ahora = time.time()
        recientes = [t for t in ANTIRAID_JOINS.get(gid, []) if ahora - t <= ventana]
        if recientes:
            await asyncio.sleep(ventana)
            continue
        break
    cfg = antiraid_db.get(gid)
    if cfg and cfg.get("active"):
        cfg["active"] = False
        cfg["activated_at"] = None
        guardar_antiraid()
        guild = bot.get_guild(int(gid))
        if guild is not None:
            embed = discord.Embed(
                title="✅ Modo raid desactivado",
                description="No se detectaron más entradas masivas. Antiraid vuelve a la normalidad.",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
            await enviar_logs(guild, embed)


@bot.command(name="antiraid")
@commands.has_permissions(manage_guild=True)
async def antiraid(ctx, *, args: str = ""):
    """
    Sistema antiraid (desactivado por defecto). Uso: .antiraid [on|off|set|action|punishnew|minage|raidmode]
    Sin argumentos muestra la configuración actual.
    """
    cfg = _antiraid_cfg(ctx.guild.id)
    tokens = args.split()
    sub = tokens[0].lower() if tokens else ""
    p = ctx.prefix if ctx.prefix and not MENTION_REGEX.match(ctx.prefix) else DEFAULT_PREFIX

    if sub in ("", "config", "status"):
        embed = _antiraid_status_embed(cfg)
        embed.set_footer(text=f"Usa {p}antiraid on para activarlo. Nunca actúa contra el staff (Manage Server).")
        return await ctx.send(embed=embed)

    if sub == "on":
        cfg["enabled"] = True
        guardar_antiraid()
        return await ctx.send("✅ Antiraid **activado**. Nunca actúa contra el staff (Manage Server), el dueño del servidor ni contra mí.")

    if sub == "off":
        cfg["enabled"] = False
        cfg["active"] = False
        cfg["activated_at"] = None
        cfg["manual"] = False
        guardar_antiraid()
        return await ctx.send("🔴 Antiraid **desactivado** (estado por defecto). Modo raid cancelado si estaba activo.")

    if sub == "set":
        if len(tokens) < 3:
            return await ctx.send("❌ Uso correcto: `.antiraid set <joins> <segundos>` (ej: `.antiraid set 5 10`)")
        try:
            umbral = int(tokens[1])
            ventana = int(tokens[2])
        except ValueError:
            return await ctx.send("❌ Ambos valores deben ser números enteros.")
        if not (2 <= umbral <= 100):
            return await ctx.send("❌ El umbral debe estar entre 2 y 100 joins.")
        if not (3 <= ventana <= 3600):
            return await ctx.send("❌ La ventana debe estar entre 3 y 3600 segundos.")
        cfg["threshold"] = umbral
        cfg["seconds"] = ventana
        guardar_antiraid()
        return await ctx.send(f"✅ Detección de raid: **{umbral} joins en {ventana}s**.")

    if sub == "action":
        if len(tokens) < 2 or tokens[1].lower() not in ("ban", "kick", "mute"):
            return await ctx.send("❌ Uso correcto: `.antiraid action <ban|kick|mute>`")
        cfg["action"] = tokens[1].lower()
        guardar_antiraid()
        extra = f" (timeout de {ANTIRAID_MUTE_MINUTOS} min)" if cfg["action"] == "mute" else ""
        return await ctx.send(f"✅ Acción antiraid: **{cfg['action']}**{extra}.")

    if sub in ("punishnew", "punish"):
        if len(tokens) < 2:
            return await ctx.send("❌ Uso correcto: `.antiraid punishnew <true|false>`")
        valor = tokens[1].lower()
        if valor in ("true", "on", "si", "sí", "1"):
            cfg["punish_new"] = True
        elif valor in ("false", "off", "no", "0"):
            cfg["punish_new"] = False
        else:
            return await ctx.send("❌ Uso correcto: `.antiraid punishnew <true|false>`")
        guardar_antiraid()
        extra = "" if cfg["punish_new"] else "\n(Con `false` solo se castiga al usuario que dispara el umbral)."
        return await ctx.send(f"✅ Castigar entradas durante raid: **{'Sí' if cfg['punish_new'] else 'No'}**.{extra}")

    if sub == "minage":
        if len(tokens) < 2:
            return await ctx.send("❌ Uso correcto: `.antiraid minage <minutos>` (0 = desactivado)")
        try:
            minutos = int(tokens[1])
        except ValueError:
            return await ctx.send("❌ Los minutos deben ser un número entero.")
        if not (0 <= minutos <= 43800):
            return await ctx.send("❌ La edad mínima debe estar entre 0 (desactivado) y 43800 minutos (~1 mes).")
        cfg["min_age"] = minutos
        guardar_antiraid()
        texto = f"**{minutos} min**" if minutos > 0 else "**desactivado**"
        return await ctx.send(f"✅ Edad mínima de cuenta: {texto}.")

    if sub in ("raidmode", "raid"):
        if len(tokens) < 2 or tokens[1].lower() not in ("on", "off"):
            return await ctx.send("❌ Uso correcto: `.antiraid raidmode <on|off>`")
        if tokens[1].lower() == "on":
            if not cfg.get("enabled"):
                return await ctx.send("❌ El antiraid está desactivado. Actívalo primero con `.antiraid on`.")
            cfg["active"] = True
            cfg["activated_at"] = time.time()
            cfg["manual"] = True
            guardar_antiraid()
            embed = discord.Embed(
                title="🚨 Modo raid activado (manual)",
                description="Todos los que entren ahora serán castigados hasta que lo desactives.",
                color=discord.Color.dark_red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text=f"Se mantiene activo hasta {p}antiraid raidmode off (no se desactiva solo).")
            await ctx.send(embed=embed)
            await enviar_logs(ctx.guild, embed)
            return
        cfg["active"] = False
        cfg["activated_at"] = None
        cfg["manual"] = False
        guardar_antiraid()
        embed = discord.Embed(title="✅ Modo raid desactivado", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        await ctx.send(embed=embed)
        await enviar_logs(ctx.guild, embed)
        return

    return await ctx.send(
        "❌ Subcomando desconocido. Usa:\n"
        f"`{p}antiraid` :: Ver configuración\n"
        f"`{p}antiraid on|off` :: Activar / desactivar\n"
        f"`{p}antiraid set <joins> <segundos>` :: Umbral de detección\n"
        f"`{p}antiraid action <ban|kick|mute>` :: Acción contra raiders\n"
        f"`{p}antiraid punishnew <true|false>` :: Castigar entradas en raid\n"
        f"`{p}antiraid minage <minutos>` :: Edad mínima de cuenta (0 = off)\n"
        f"`{p}antiraid raidmode <on|off>` :: Modo raid manual"
    )


@antiraid.error
async def antiraid_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Server.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ Me faltan permisos para ejecutar este comando.")


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
async def softban(ctx, *, args: str = ""):
    """
    Banea temporalmente a un usuario.
    Uso normal: .softban <id|@|nombre> <duracion> [motivo]
    Si respondes al mensaje del usuario: .softban <duracion> [motivo]
    """
    tokens = args.split(maxsplit=2)
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)

    if usuario_repl is not None:
        if not tokens:
            return await ctx.send("❌ Debes indicar una duración.\nUso: `.softban <duración> [motivo]` (respondiendo al mensaje)")
        usuario, miembro = usuario_repl, miembro_repl
        duracion = tokens[0]
        motivo = tokens[1] if len(tokens) > 1 else "No especificado"
    else:
        if len(tokens) < 2:
            return await ctx.send("❌ Debes indicar un usuario y una duración, o responder al mensaje del usuario.\nUso correcto: `.softban <id|@|nombre> <duración> [motivo]`")
        usuario_arg, duracion = tokens[0], tokens[1]
        motivo = tokens[2] if len(tokens) > 2 else "No especificado"
        usuario, miembro, err = await resolver_usuario(ctx.guild, usuario_arg)
        if err:
            return await ctx.send(err)

    segundos, err = parsear_duracion(duracion)
    if err:
        return await ctx.send(f"❌ {err}")
    if segundos > 86400 * 7:
        return await ctx.send("❌ La duración máxima de un softban es de 7 días.")

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
        await ctx.send("❌ Uso correcto: `.softban <id|@|nombre> <duración> [motivo]` o, respondiendo, `.softban <duración> [motivo]`")
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
    prefijo = get_prefix_message(ctx.guild)  # versión con backticks, solo para mostrar en texto
    p = ctx.prefix if ctx.prefix and not MENTION_REGEX.match(ctx.prefix) else DEFAULT_PREFIX  # prefijo "crudo", para meter dentro de bloques de código
    if comando is not None:
        cmd = bot.get_command(comando.lower())
        if cmd is None:
            return await ctx.send(f"❌ No existe el comando `{comando}`.")
        embed = discord.Embed(title=f"Ayuda: {cmd.name}", color=discord.Color.blurple())
        embed.add_field(name="Descripción", value=cmd.help or "Sin descripción.", inline=False)
        embed.add_field(name="Uso", value=f"`{p}{cmd.signature}`", inline=False)
        if cmd.aliases:
            embed.add_field(name="Aliases", value=", ".join(cmd.aliases), inline=False)
        return await ctx.send(embed=embed)

    # Embed inicial con selector
    embed = discord.Embed(
        title="Lista de comandos",
        description=f"Prefijo actual: {prefijo}\nSelecciona una categoría en el menú desplegable para ver sus comandos.\n\nTambién puedes usar slash commands `/` y mencionar al bot.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Categorías", value=(
        f"`{p}help mod` :: Moderación\n"
        f"`{p}help antiraid` :: Antiraid\n"
        f"`{p}help roles` :: Roles\n"
        f"`{p}help niveles` :: Niveles / XP\n"
        f"`{p}help economia` :: Economía\n"
        f"`{p}help sorteos` :: Sorteos y utilidades\n"
        f"`{p}help canales` :: Canales y links\n"
        f"`{p}help config` :: Configuración"
    ), inline=False)
    embed.set_footer(text=f"Usa {p}help <comando> para ver el detalle de un comando específico. Selecciona una categoría abajo")

    class HelpView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.select(
            placeholder="Selecciona una categoría...",
            options=[
                discord.SelectOption(label="Moderación", value="mod", description="Ban, kick, mute, warn, purge, nuke, etc."),
                discord.SelectOption(label="Antiraid", value="antiraid", description="Antiraid on/off, set, action, punishnew, minage, raidmode"),
                discord.SelectOption(label="Roles", value="roles", description="Roleadd, roleremove, rolehuman, autorole, etc."),
                discord.SelectOption(label="Niveles / XP", value="niveles", description="Rank, level, leaderboard, level-config, etc."),
                discord.SelectOption(label="Economía", value="economia", description="Balance, work, crime, rob, tienda, juegos, etc."),
                discord.SelectOption(label="Sorteos y utilidades", value="sorteos", description="Giveaways, avatar, banner, remindme, etc."),
                discord.SelectOption(label="Canales y links", value="canales", description="Linkban, logchannel, honeypot, etc."),
                discord.SelectOption(label="Configuración", value="config", description="Setprefix, prefix, prefixremove, sync, etc."),
            ],
            min_values=1,
            max_values=1,
        )
        async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
            if interaction.user != ctx.author:
                return await interaction.response.send_message("❌ Solo quien ejecutó el comando puede usar el menú.", ephemeral=True)
            
            category = select.values[0]
            
            embeds = {
                "mod": discord.Embed(title="Moderación", description=(
                    f"`{p}ban (@usuario) [motivo]` :: Banea usuario\n"
                    f"`{p}kick (@usuario) [motivo]` :: Expulsa usuario\n"
                    f"`{p}unban (id) [motivo]` :: Desbanea usuario\n"
                    f"`{p}mute (@usuario) (duración) [motivo]` :: Silencia (ej: 5m, 1h)\n"
                    f"`{p}unmute (@usuario) [motivo]` :: Quita silencio\n"
                    f"`{p}softban (@usuario) (duración) [motivo]` :: Ban temporal\n"
                    f"`{p}ipban (@usuario) [motivo]` :: Ban + veto IP\n"
                    f"`{p}ipunban (@usuario)` :: Desbanea IP\n"
                    f"`{p}purge (cantidad)` :: Borra mensajes\n"
                    f"`{p}nuke [#canal]` :: Nuke canal con confirmación\n"
                    f"`{p}lock [#canal]` :: Lockea canal\n"
                    f"`{p}unlock [#canal]` :: Desbloquea canal\n"
                    f"`{p}rename (@usuario) (apodo)` :: Cambia apodo\n"
                    f"`{p}namereset (@usuario)` :: Resetea apodo\n"
                    f"`{p}warn (@usuario) (motivo)` :: Advierte usuario\n"
                    f"`{p}warnremove (@usuario) (número)` :: Quita warn\n"
                    f"`{p}warns (@usuario)` :: Ver warns"
                ), color=discord.Color.red()),
                "antiraid": discord.Embed(title="Antiraid", description=(
                    f"`{p}antiraid` :: Ver configuración\n"
                    f"`{p}antiraid on/off` :: Activar / desactivar\n"
                    f"`{p}antiraid set (joins) (segundos)` :: Umbral de raid\n"
                    f"`{p}antiraid action <ban|kick|mute>` :: Acción contra raiders\n"
                    f"`{p}antiraid punishnew <true|false>` :: Castigar entradas en raid\n"
                    f"`{p}antiraid minage (minutos)` :: Edad mínima de cuenta\n"
                    f"`{p}antiraid raidmode <on|off>` :: Modo raid manual\n\n"
                    f"Desactivado por defecto • Nunca actúa contra el staff (Manage Server)"
                ), color=discord.Color.dark_red()),
                "roles": discord.Embed(title="Roles", description=(
                    f"`{p}roleadd (@usuario) (@rol)` :: Otorga rol\n"
                    f"`{p}roleremove (@usuario) (@rol)` :: Quita rol\n"
                    f"`{p}rolehuman (@rol)` :: Rol a todos humanos\n"
                    f"`{p}roleall (@rol)` :: Rol a todos (humanos+bots)\n"
                    f"`{p}rolebot (@rol)` :: Rol solo a bots\n"
                    f"`{p}autorolehuman (@rol)` :: Autorol on/off para humanos\n"
                    f"`{p}autorolebot (@rol)` :: Autorol on/off para bots\n"
                    f"`{p}autorole (@rol)` :: Autorol on/off para todos\n"
                    f"`{p}autorolelist` :: Lista autoroles activos"
                ), color=discord.Color.blue()),
                "niveles": discord.Embed(title="Niveles / XP", description=(
                    f"`{p}rank (@usuario)` :: Rango con barra de progreso\n"
                    f"`{p}level/nivel (@usuario)` :: Info de nivel\n"
                    f"`{p}leaderboard/lb/ranking [página]` :: Ranking paginado\n"
                    f"`{p}level-config enabled (true/false)` :: Activar/desactivar\n"
                    f"`{p}level-config xp (min) (max)` :: Rango XP por mensaje\n"
                    f"`{p}level-config cooldown (segundos)` :: Anti-spam\n"
                    f"`{p}level-config channel (#canal)` :: Canal anuncios\n"
                    f"`{p}level-config message (texto)` :: Mensaje level-up\n"
                    f"`{p}level-config announce (true/false)` :: Anuncios on/off\n"
                    f"`{p}set-level-role (nivel) (@rol)` :: Rol por nivel\n"
                    f"`{p}remove-level-role (nivel)` :: Quita recompensa\n"
                    f"`{p}set-xp (@usuario) (cantidad)` :: Establece XP\n"
                    f"`{p}set-level (@usuario) (nivel)` :: Establece nivel\n"
                    f"`{p}add-xp (@usuario) (cantidad)` :: Añade XP\n"
                    f"`{p}remove-xp (@usuario) (cantidad)` :: Quita XP\n"
                    f"`{p}reset-level (@usuario)` :: Resetea XP/nivel"
                ), color=discord.Color.gold()),
                "economia": discord.Embed(title="Economía", description=(
                    f"`{p}balance/bal (@usuario)` :: Ver tu dinero\n"
                    f"`{p}pay @usuario <monto|all>` :: Pagar a alguien\n"
                    f"`{p}daily` :: Recompensa diaria\n"
                    f"`{p}weekly` :: Recompensa semanal\n"
                    f"`{p}monthly` :: Recompensa mensual\n"
                    f"`{p}work` :: Trabaja (1h cooldown)\n"
                    f"`{p}crime` :: Crimen: gana o cárcel 🚔\n"
                    f"`{p}slut` :: Dinero fácil (arriesgado)\n"
                    f"`{p}rob @usuario` :: Roba efectivo\n"
                    f"`{p}deposit <monto|all>` :: Ingresar al banco\n"
                    f"`{p}withdraw <monto|all>` :: Sacar del banco\n"
                    f"`{p}shop` :: Ver tienda\n"
                    f"`{p}shop add <item> <precio> [desc]` :: Añadir item (admin)\n"
                    f"`{p}shop remove <item>` :: Quitar item (admin)\n"
                    f"`{p}buy <item> [cantidad]` :: Comprar item\n"
                    f"`{p}sell <item> [cantidad]` :: Vender item (50%)\n"
                    f"`{p}inventory (@usuario)` :: Tu inventario\n"
                    f"`{p}use <item>` :: Usar item\n"
                    f"`{p}gift @usuario <item> [cantidad]` :: Regalar item\n"
                    f"`{p}slots <monto|all>` :: Tragaperras\n"
                    f"`{p}coinflip <cara|cruz> <monto|all>` :: Moneda (x1.95)\n"
                    f"`{p}dice <1-6> <monto|all>` :: Dado (x5)\n"
                    f"`{p}highlow <monto|all>` :: Mayor o menor (x1.9)\n"
                    f"`{p}roulette <rojo|negro|verde> <monto|all>` :: Ruleta\n"
                    f"`{p}blackjack <monto|all>` :: Blackjack (x2, natural x2.5)\n"
                    f"`{p}baltop` :: Top de ricos\n"
                    f"`{p}add-money @usuario <monto>` :: Dar dinero (admin)\n"
                    f"`{p}remove-money @usuario <monto>` :: Quitar dinero (admin)\n"
                    f"`{p}set-money @usuario <monto>` :: Fijar dinero (admin)\n"
                    f"`{p}set-currency <símbolo>` :: Símbolo de moneda (admin)\n"
                    f"`{p}set-start-balance <monto>` :: Balance inicial (admin)\n"
                    f"`{p}economy-config` :: Ver configuración (admin)\n"
                    f"`{p}reset-economy confirmar` :: Resetear economía (admin)"
                ), color=discord.Color.green()),
                "sorteos": discord.Embed(title="Sorteos y utilidades", description=(
                    f"`{p}gcreate (nombre) (duración) (ganadores)` :: Crear sorteo\n"
                    f"`{p}glist` :: Lista sorteos\n"
                    f"`{p}gdelete (número)` :: Eliminar sorteo\n"
                    f"`{p}greroll (número)` :: Re-rollear ganadores\n"
                    f"`{p}avatar (@usuario)` :: Avatar 4K\n"
                    f"`{p}banner (@usuario)` :: Banner 4K\n"
                    f"`{p}remindme (duración) (mensaje) (MD: sí/no)` :: Recordatorio"
                ), color=discord.Color.purple()),
                "canales": discord.Embed(title="Canales y links", description=(
                    f"`{p}linkban (#canal)` :: Prohíbe enlaces\n"
                    f"`{p}linkunban (#canal)` :: Permite enlaces\n"
                    f"`{p}linkbanlist` :: Lista canales sin links\n"
                    f"`{p}logchannel (#canal)` :: Canal de logs\n"
                    f"`{p}logunchannel (#canal)` :: Quita canal logs\n"
                    f"`{p}logschannels` :: Lista canales logs\n"
                    f"`{p}honeypot (#canal)` :: Crea honeypot (ban)\n"
                    f"`{p}honeypots` :: Lista honeypots\n"
                    f"`{p}honeypotset (#canal) ban|kick|mute [duración]` :: Config honeypot\n"
                    f"`{p}starboard` :: Ver config del starboard\n"
                    f"`{p}starreactions <número>` :: Cambia estrellas necesarias\n"
                    f"`{p}starenable` :: Activa starboard (elige canal)\n"
                    f"`{p}stardisable` :: Desactiva starboard"
                ), color=discord.Color.teal()),
                "config": discord.Embed(title="Configuración", description=(
                    f"`{p}setprefix (carácter)` :: Añade prefijo (máx 5 chars)\n"
                    f"`{p}prefix` :: Ver prefijos activos\n"
                    f"`{p}prefixremove (carácter)` :: Quita prefijo\n"
                    f"`{p}sync` :: Sincroniza slash commands (owner)\n"
                    f"`{p}dashboard` :: URL del panel web\n"
                    f"`{p}help [comando]` :: Esta ayuda"
                ), color=discord.Color.dark_gray()),
            }
            
            embed = embeds[category]
            embed.set_footer(text=f"Prefijo: {p} • Usa {p}help <comando> para más detalles")
            await interaction.response.edit_message(embed=embed, view=self)

    view = HelpView()
    await ctx.send(embed=embed, view=view)


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
#  AUTOROLE (evento al unirse un miembro)
# ============================================================

@bot.event
async def on_member_join(member: discord.Member):
    """Antiraid + asignación automática de autoroles cuando alguien se une al servidor."""
    # Antiraid: si el miembro fue castigado por el sistema, no se le aplican autoroles.
    if await _antiraid_check(member):
        return
    gid = str(member.guild.id)
    config = autoroles_db.get(gid)
    if not config:
        return

    categorias = ["all", "bot" if member.bot else "human"]
    roles_a_dar = []
    for categoria in categorias:
        for rid in config.get(categoria, []):
            rol = member.guild.get_role(int(rid))
            if rol is not None and rol not in member.roles:
                roles_a_dar.append(rol)

    if not roles_a_dar:
        return

    try:
        await member.add_roles(*roles_a_dar, reason="Autorole al unirse al servidor")
    except (discord.Forbidden, discord.HTTPException):
        pass


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
#  GRUPO: /autorole ...
# ============================================================

autorole_group = app_commands.Group(name="autorole", description="Gestión de autoroles (roles automáticos al unirse)")


@autorole_group.command(name="human", description="Añade o quita un autorol para miembros humanos")
@app_commands.describe(rol="Rol a alternar como autorol de humanos")
@app_commands.default_permissions(manage_roles=True)
async def slash_autorole_human(interaction: discord.Interaction, rol: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ No tienes permiso para gestionar roles.", ephemeral=True)
    error = _autorole_check_permisos(interaction.user, interaction.guild, rol)
    if error:
        return await interaction.response.send_message(error, ephemeral=True)
    accion = _toggle_autorole(interaction.guild, rol, "human")
    await interaction.response.send_message(f"✅ Rol {rol.mention} **{accion}** como autorol para **miembros humanos**.")


@autorole_group.command(name="bot", description="Añade o quita un autorol para bots")
@app_commands.describe(rol="Rol a alternar como autorol de bots")
@app_commands.default_permissions(manage_roles=True)
async def slash_autorole_bot(interaction: discord.Interaction, rol: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ No tienes permiso para gestionar roles.", ephemeral=True)
    error = _autorole_check_permisos(interaction.user, interaction.guild, rol)
    if error:
        return await interaction.response.send_message(error, ephemeral=True)
    accion = _toggle_autorole(interaction.guild, rol, "bot")
    await interaction.response.send_message(f"✅ Rol {rol.mention} **{accion}** como autorol para **bots**.")


@autorole_group.command(name="general", description="Añade o quita un autorol para TODOS los miembros (humanos y bots)")
@app_commands.describe(rol="Rol a alternar como autorol general")
@app_commands.default_permissions(manage_roles=True)
async def slash_autorole_general(interaction: discord.Interaction, rol: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ No tienes permiso para gestionar roles.", ephemeral=True)
    error = _autorole_check_permisos(interaction.user, interaction.guild, rol)
    if error:
        return await interaction.response.send_message(error, ephemeral=True)
    accion = _toggle_autorole(interaction.guild, rol, "all")
    await interaction.response.send_message(f"✅ Rol {rol.mention} **{accion}** como autorol para **todos los miembros**.")


@autorole_group.command(name="list", description="Lista los autoroles configurados en el servidor")
@app_commands.default_permissions(manage_roles=True)
async def slash_autorole_list(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ No tienes permiso para gestionar roles.", ephemeral=True)
    embed = _construir_embed_autorolelist(interaction.guild)
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(autorole_group)


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
#  SLASH: /antiraid (grupo)
# ============================================================

antiraid_group = app_commands.Group(name="antiraid", description="Sistema antiraid (desactivado por defecto)")


@antiraid_group.command(name="config", description="Muestra la configuración actual del antiraid")
@app_commands.default_permissions(manage_guild=True)
async def slash_antiraid_config(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    cfg = _antiraid_cfg(interaction.guild.id)
    embed = _antiraid_status_embed(cfg)
    embed.set_footer(text="Usa /antiraid on para activarlo. Nunca actúa contra el staff (Manage Server).")
    await interaction.response.send_message(embed=embed)


@antiraid_group.command(name="on", description="Activa el sistema antiraid")
@app_commands.default_permissions(manage_guild=True)
async def slash_antiraid_on(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    cfg = _antiraid_cfg(interaction.guild.id)
    cfg["enabled"] = True
    guardar_antiraid()
    await interaction.response.send_message("✅ Antiraid **activado**. Nunca actúa contra el staff (Manage Server), el dueño del servidor ni contra mí.")


@antiraid_group.command(name="off", description="Desactiva el sistema antiraid (estado por defecto)")
@app_commands.default_permissions(manage_guild=True)
async def slash_antiraid_off(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    cfg = _antiraid_cfg(interaction.guild.id)
    cfg["enabled"] = False
    cfg["active"] = False
    cfg["activated_at"] = None
    cfg["manual"] = False
    guardar_antiraid()
    await interaction.response.send_message("🔴 Antiraid **desactivado** (estado por defecto). Modo raid cancelado si estaba activo.")


@antiraid_group.command(name="set", description="Configura el umbral de detección de raid")
@app_commands.describe(joins="Joins necesarios para considerar raid (2-100)", segundos="Ventana de tiempo en segundos (3-3600)")
@app_commands.default_permissions(manage_guild=True)
async def slash_antiraid_set(interaction: discord.Interaction, joins: int, segundos: int):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    if not (2 <= joins <= 100):
        return await interaction.response.send_message("❌ El umbral debe estar entre 2 y 100 joins.", ephemeral=True)
    if not (3 <= segundos <= 3600):
        return await interaction.response.send_message("❌ La ventana debe estar entre 3 y 3600 segundos.", ephemeral=True)
    cfg = _antiraid_cfg(interaction.guild.id)
    cfg["threshold"] = joins
    cfg["seconds"] = segundos
    guardar_antiraid()
    await interaction.response.send_message(f"✅ Detección de raid: **{joins} joins en {segundos}s**.")


@antiraid_group.command(name="action", description="Acción contra los raiders")
@app_commands.describe(accion="Acción a aplicar")
@app_commands.default_permissions(manage_guild=True)
@app_commands.choices(accion=[
    app_commands.Choice(name="ban", value="ban"),
    app_commands.Choice(name="kick", value="kick"),
    app_commands.Choice(name="mute (timeout 1h)", value="mute"),
])
async def slash_antiraid_action(interaction: discord.Interaction, accion: app_commands.Choice[str]):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    cfg = _antiraid_cfg(interaction.guild.id)
    cfg["action"] = accion.value
    guardar_antiraid()
    extra = f" (timeout de {ANTIRAID_MUTE_MINUTOS} min)" if cfg["action"] == "mute" else ""
    await interaction.response.send_message(f"✅ Acción antiraid: **{cfg['action']}**{extra}.")


@antiraid_group.command(name="punishnew", description="Castigar a quienes entren durante un raid activo")
@app_commands.describe(valor="True = castigar a todos los que entren en raid")
@app_commands.default_permissions(manage_guild=True)
async def slash_antiraid_punishnew(interaction: discord.Interaction, valor: bool):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    cfg = _antiraid_cfg(interaction.guild.id)
    cfg["punish_new"] = valor
    guardar_antiraid()
    extra = "" if valor else "\n(Con `false` solo se castiga al usuario que dispara el umbral)."
    await interaction.response.send_message(f"✅ Castigar entradas durante raid: **{'Sí' if valor else 'No'}**.{extra}")


@antiraid_group.command(name="minage", description="Edad mínima de la cuenta para entrar (anti-cuentas nuevas)")
@app_commands.describe(minutos="Edad mínima en minutos (0 = desactivado, máx 43800)")
@app_commands.default_permissions(manage_guild=True)
async def slash_antiraid_minage(interaction: discord.Interaction, minutos: int):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    if not (0 <= minutos <= 43800):
        return await interaction.response.send_message("❌ La edad mínima debe estar entre 0 (desactivado) y 43800 minutos (~1 mes).", ephemeral=True)
    cfg = _antiraid_cfg(interaction.guild.id)
    cfg["min_age"] = minutos
    guardar_antiraid()
    texto = f"**{minutos} min**" if minutos > 0 else "**desactivado**"
    await interaction.response.send_message(f"✅ Edad mínima de cuenta: {texto}.")


@antiraid_group.command(name="raidmode", description="Activa/desactiva el modo raid manualmente")
@app_commands.describe(estado="on = castigar todas las entradas hasta que lo apagues")
@app_commands.default_permissions(manage_guild=True)
@app_commands.choices(estado=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
])
async def slash_antiraid_raidmode(interaction: discord.Interaction, estado: app_commands.Choice[str]):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    cfg = _antiraid_cfg(interaction.guild.id)
    if estado.value == "on":
        if not cfg.get("enabled"):
            return await interaction.response.send_message("❌ El antiraid está desactivado. Actívalo primero con `/antiraid on`.", ephemeral=True)
        cfg["active"] = True
        cfg["activated_at"] = time.time()
        cfg["manual"] = True
        guardar_antiraid()
        embed = discord.Embed(
            title="🚨 Modo raid activado (manual)",
            description="Todos los que entren ahora serán castigados hasta que lo desactives.",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Se mantiene activo hasta /antiraid raidmode off (no se desactiva solo).")
        await interaction.response.send_message(embed=embed)
        await enviar_logs(interaction.guild, embed)
        return
    cfg["active"] = False
    cfg["activated_at"] = None
    cfg["manual"] = False
    guardar_antiraid()
    embed = discord.Embed(title="✅ Modo raid desactivado", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    await interaction.response.send_message(embed=embed)
    await enviar_logs(interaction.guild, embed)


bot.tree.add_command(antiraid_group)



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
    embed.add_field(name="🚨 Antiraid", value="`antiraid` (ver config) `antiraid on`/`off` `antiraid set` `antiraid action` `antiraid punishnew` `antiraid minage` `antiraid raidmode`\nGrupo slash `/antiraid`: config, on, off, set, action, punishnew, minage, raidmode.\nDesactivado por defecto • Nunca actúa contra el staff (Manage Server)", inline=False)
    embed.add_field(name="👥 Roles", value="`roleadd`/`role add` `roleremove`/`role remove` `rolehuman`/`role human` `roleall`/`role all` `rolebot`/`role bot`\n`autorolehuman`/`autorole human` `autorolebot`/`autorole bot` `autorole`/`autorole general` `autorolelist`/`autorole list`", inline=False)
    embed.add_field(name="📊 Niveles / XP", value="`/level rank [usuario]` `/level levels [usuario]` `/level leaderboard [página]`\n`/level-admin config enabled/xp/cooldown/channel/message/announce`\n`/level-admin set-role/remove-role/set-xp/set-level/add-xp/remove-xp/reset`", inline=False)
    embed.add_field(name="💰 Economía", value="`balance` `pay` `daily` `weekly` `monthly` `work` `crime` `slut` `rob`\n`deposit` `withdraw` `shop`/`shop-add`/`shop-remove` `buy` `sell` `inventory` `use` `gift`\n`slots` `coinflip` `dice` `highlow` `roulette` `blackjack` `baltop`\n`add-money` `remove-money` `set-money` `set-currency` `set-start-balance` `economy-config` `reset-economy`", inline=False)
    embed.add_field(name="🎉 Sorteos y utilidades", value="`gcreate`/`giveaway create` `glist`/`giveaway list` `gdelete`/`giveaway delete` `greroll`/`giveaway reroll` `avatar` `banner` `remindme`/`remind`", inline=False)
    embed.add_field(name="🔗 Canales y links", value="`linkban`/`link ban` `linkunban`/`link unban` `linkbanlist`/`link list` `logchannel`/`log channel` `logunchannel`/`log unchannel` `logschannels`/`log channels`", inline=False)
    embed.add_field(name="⚙️ Configuración", value=f"`setprefix`/`/setprefix` `prefix` `prefixremove` `sync` `dashboard` `help`", inline=False)
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


async def _send_rank(ctx_or_interaction, target: discord.Member):
    """Función compartida para enviar el rank (prefix y slash)."""
    if target.bot:
        return await _send(ctx_or_interaction, "❌ Los bots no tienen nivel.", ephemeral=True)
    user_data = get_user_xp(ctx_or_interaction.guild.id, target.id)
    xp_in_level, xp_needed, progress = get_xp_progress(user_data)
    rank = get_user_rank(ctx_or_interaction.guild.id, target.id)
    total_users = len(get_guild_leaderboard(ctx_or_interaction.guild.id))
    bar = create_progress_bar(progress, 20)
    xp_total_next = xp_for_level(user_data["level"] + 1)
    
    embed = discord.Embed(title=f"📊 Rango de {target.display_name}", color=target.color or discord.Color.blurple())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="📈 Nivel", value=f"**{user_data['level']}**", inline=True)
    embed.add_field(name="⭐ XP Total", value=f"**{user_data['xp']:,}**", inline=True)
    embed.add_field(name="🏆 Posición", value=f"**#{rank}** de {total_users}", inline=True)
    embed.add_field(name="", value="", inline=False)
    embed.add_field(
        name=f"Progreso hacia nivel {user_data['level'] + 1}",
        value=f"`{bar}` {progress:.1f}%\n**{xp_in_level:,} / {xp_needed:,} XP**",
        inline=False
    )
    embed.set_footer(text=f"XP en este nivel: {xp_in_level:,} / {xp_needed:,} | XP total para siguiente nivel: {xp_for_level(user_data['level'] + 1):,}")
    await _send(ctx_or_interaction, embed=embed)


async def _send_levels(ctx_or_interaction, target: discord.Member):
    """Función compartida para levels."""
    if target.bot:
        return await _send(ctx_or_interaction, "❌ Los bots no tienen nivel.", ephemeral=True)
    user_data = get_user_xp(ctx_or_interaction.guild.id, target.id)
    xp_in_level, xp_needed, progress = get_xp_progress(user_data)
    rank = get_user_rank(ctx_or_interaction.guild.id, target.id)
    total_users = len(get_guild_leaderboard(ctx_or_interaction.guild.id))
    
    embed = discord.Embed(title=f"📊 Nivel de {target.display_name}", color=target.color or discord.Color.blurple())
    embed.add_field(name="Nivel actual", value=f"**{user_data['level']}**", inline=True)
    embed.add_field(name="XP actual", value=f"**{user_data['xp']:,}**", inline=True)
    embed.add_field(name="XP para siguiente nivel", value=f"**{xp_needed:,}**", inline=True)
    embed.add_field(name="Progreso", value=f"{progress:.1f}%", inline=True)
    embed.add_field(name="Posición en ranking", value=f"#{rank} de {total_users}", inline=True)
    await _send(ctx_or_interaction, embed=embed, ephemeral=True)


async def _send_leaderboard(ctx_or_interaction, page: int = 1):
    leaderboard = get_guild_leaderboard(ctx_or_interaction.guild.id)
    if not leaderboard:
        return await _send(ctx_or_interaction, "ℹ️ No hay datos de XP en este servidor.", ephemeral=True)
    
    view = LeaderboardView(ctx_or_interaction.guild, leaderboard)
    page = max(1, min(page, view.max_page + 1))
    view.current_page = page - 1
    view.update_buttons()
    await _send(ctx_or_interaction, embed=view.get_embed(), view=view)


def _send(ctx_or_interaction, content=None, embed=None, view=None, ephemeral=False):
    """Helper unificado para enviar mensajes (funciona con Context e Interaction)."""
    async def _inner():
        if isinstance(ctx_or_interaction, discord.Interaction):
            if ctx_or_interaction.response.is_done():
                await ctx_or_interaction.followup.send(content=content, embed=embed, view=view, ephemeral=ephemeral)
            else:
                await ctx_or_interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=ephemeral)
        else:
            if ephemeral:
                try:
                    await ctx_or_interaction.send(content=content, embed=embed, view=view, delete_after=10)
                except Exception:
                    pass
            else:
                await ctx_or_interaction.send(content=content, embed=embed, view=view)
    return _inner()


# ============================================================
#  PREFIX: .level (Sistema de Niveles/XP)
# ============================================================

@bot.command(name="rank", help="Muestra el rango y nivel de un usuario. Uso: .rank (@usuario)")
async def prefix_rank(ctx, *, usuario: discord.Member = None):
    """Muestra el rango y nivel de un usuario. Uso: .rank (@usuario)"""
    target = usuario or ctx.author
    await _send_rank(ctx, target)


@bot.command(name="level", aliases=["nivel"], help="Muestra información del nivel. Uso: .level (@usuario)")
async def prefix_level(ctx, *, usuario: discord.Member = None):
    """Muestra información del sistema de niveles. Uso: .level (@usuario)"""
    target = usuario or ctx.author
    await _send_levels(ctx, target)


@bot.command(name="leaderboard", aliases=["lb", "ranking"], help="Muestra el ranking. Uso: .leaderboard [página]")
async def prefix_leaderboard(ctx, pagina: int = 1):
    """Muestra el ranking de usuarios con más XP. Uso: .leaderboard [página]"""
    await _send_leaderboard(ctx, pagina)


# ============================================================
#  PREFIX: .level-admin (Administración del sistema de niveles)
# ============================================================

@bot.command(name="level-config", help="Configura el sistema de niveles. Uso: .level-config (opción) (valor)")
@commands.has_permissions(manage_guild=True)
async def prefix_level_config(
    ctx,
    opcion: str = None,
    valor1: str = None,
    valor2: str = None
):
    """Configura el sistema de niveles.
    Uso: .level-config (opción) (valor)
    Opciones: enabled (true/false), xp (min max), cooldown (segundos), channel (#canal), message (texto), announce (true/false)
    """
    if opcion is None:
        return await ctx.send("❌ Uso: `.level-config (opción) (valor)`\nOpciones: `enabled (true/false)`, `xp (min max)`, `cooldown (segundos)`, `channel (#canal)`, `message (texto)`, `announce (true/false)`")
    
    gid = str(ctx.guild.id)
    config = get_xp_config(ctx.guild.id)
    opcion = opcion.lower()
    
    if opcion == "enabled":
        if valor1 is None or valor1.lower() not in ("true", "false"):
            return await ctx.send("❌ Uso: `.level-config enabled (true/false)`")
        config["enabled"] = valor1.lower() == "true"
    elif opcion == "xp":
        if valor1 is None or valor2 is None:
            return await ctx.send("❌ Uso: `.level-config xp (XP mínimo) (XP máximo)`")
        try:
            xp_min = int(valor1)
            xp_max = int(valor2)
        except ValueError:
            return await ctx.send("❌ Los valores deben ser números enteros.")
        if xp_min < 0 or xp_max < 0:
            return await ctx.send("❌ El XP no puede ser negativo.")
        if xp_min > xp_max:
            return await ctx.send("❌ El XP mínimo no puede ser mayor que el máximo.")
        config["xp_min"] = xp_min
        config["xp_max"] = xp_max
    elif opcion == "cooldown":
        if valor1 is None:
            return await ctx.send("❌ Uso: `.level-config cooldown (segundos)`")
        try:
            cooldown = int(valor1)
        except ValueError:
            return await ctx.send("❌ El cooldown debe ser un número entero.")
        if cooldown < 0:
            return await ctx.send("❌ El cooldown no puede ser negativo.")
        config["cooldown"] = cooldown
    elif opcion in ("channel", "canal"):
        if valor1 is None:
            return await ctx.send("❌ Uso: `.level-config channel (#canal)`")
        # Intentar parsear mención de canal
        m = re.match(r"<#(\d+)>", valor1)
        if m:
            canal_id = int(m.group(1))
        else:
            try:
                canal_id = int(valor1)
            except ValueError:
                return await ctx.send("❌ Canal inválido. Menciona el canal o usa su ID.")
        canal = ctx.guild.get_channel(canal_id)
        if canal is None:
            return await ctx.send("❌ Canal no encontrado.")
        config["levelup_channel"] = canal.id
    elif opcion in ("message", "mensaje"):
        if valor1 is None:
            return await ctx.send("❌ Uso: `.level-config message (texto)`\nVariables: {user}, {level}, {xp}, {server}")
        # Unir todos los argumentos restantes como mensaje
        mensaje = " ".join([valor1] + ([valor2] if valor2 else []))
        config["levelup_msg"] = mensaje
    elif opcion in ("announce", "anuncios"):
        if valor1 is None or valor1.lower() not in ("true", "false"):
            return await ctx.send("❌ Uso: `.level-config announce (true/false)`")
        config["levelup_enabled"] = valor1.lower() == "true"
    else:
        return await ctx.send("❌ Opción inválida. Opciones: `enabled`, `xp`, `cooldown`, `channel`, `message`, `announce`")
    
    xp_config_db[str(ctx.guild.id)] = config
    guardar_xp()
    
    embed = discord.Embed(title="⚙️ Configuración de niveles actualizada", color=discord.Color.green())
    embed.add_field(name="Estado", value="✅ Activado" if config["enabled"] else "❌ Desactivado", inline=True)
    embed.add_field(name="XP por mensaje", value=f"{config['xp_min']} - {config['xp_max']}", inline=True)
    embed.add_field(name="Cooldown", value=f"{config['cooldown']}s", inline=True)
    embed.add_field(name="Canal anuncios", value=f"<#{config['levelup_channel']}>" if config["levelup_channel"] else "Canal actual", inline=True)
    embed.add_field(name="Anuncios", value="✅ Activados" if config["levelup_enabled"] else "❌ Desactivados", inline=True)
    if config["levelup_msg"]:
        embed.add_field(name="Mensaje level up", value=config["levelup_msg"], inline=False)
    await ctx.send(embed=embed)


@bot.command(name="set-level-role", help="Asigna un rol al alcanzar un nivel. Uso: .set-level-role (nivel) (@rol)")
@commands.has_permissions(manage_guild=True)
async def prefix_set_level_role(ctx, nivel: int, rol: discord.Role):
    """Asigna un rol al alcanzar un nivel. Uso: .set-level-role (nivel) (@rol)"""
    if nivel < 1:
        return await ctx.send("❌ El nivel debe ser al menos 1.")
    if rol.position >= ctx.guild.me.top_role.position:
        return await ctx.send("❌ No puedo asignar ese rol (está por encima de mi rol).")
    gid = str(ctx.guild.id)
    level_roles_db.setdefault(gid, {})
    level_roles_db[gid][str(nivel)] = str(rol.id)
    guardar_level_roles()
    await ctx.send(f"✅ Rol {rol.mention} asignado para el nivel {nivel}.")


@bot.command(name="remove-level-role", help="Elimina una recompensa de rol por nivel. Uso: .remove-level-role (nivel)")
@commands.has_permissions(manage_guild=True)
async def prefix_remove_level_role(ctx, nivel: int):
    """Elimina una recompensa de rol por nivel. Uso: .remove-level-role (nivel)"""
    gid = str(ctx.guild.id)
    if gid not in level_roles_db or str(nivel) not in level_roles_db[gid]:
        return await ctx.send("❌ No hay recompensa configurada para ese nivel.")
    del level_roles_db[gid][str(nivel)]
    if not level_roles_db[gid]:
        del level_roles_db[gid]
    guardar_level_roles()
    await ctx.send(f"✅ Recompensa de nivel {nivel} eliminada.")


@bot.command(name="set-xp", help="Establece el XP de un usuario. Uso: .set-xp (@usuario) (cantidad) o responde al usuario")
@commands.has_permissions(manage_guild=True)
async def prefix_set_xp(ctx, *, args: str = ""):
    """Establece el XP de un usuario manualmente. Respondiendo: `.set-xp (cantidad)`."""
    tokens = args.split()
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)
    if usuario_repl is not None:
        if not tokens:
            return await ctx.send("❌ Debes indicar la cantidad de XP. Uso: `.set-xp (cantidad)` respondiendo al usuario.")
        usuario = miembro_repl or usuario_repl
        try:
            xp = int(tokens[0])
        except ValueError:
            return await ctx.send("❌ La cantidad de XP debe ser un número entero.")
    else:
        if len(tokens) < 2:
            return await ctx.send("❌ Uso correcto: `.set-xp (@usuario) (cantidad)` o responde al usuario.")
        usuario, _, err = await resolver_usuario(ctx.guild, tokens[0])
        if err:
            return await ctx.send(err)
        try:
            xp = int(tokens[1])
        except ValueError:
            return await ctx.send("❌ La cantidad de XP debe ser un número entero.")
    if xp < 0:
        return await ctx.send("❌ El XP no puede ser negativo.")
    if usuario.bot:
        return await ctx.send("❌ No se puede modificar XP de bots.")
    user_data = get_user_xp(ctx.guild.id, usuario.id)
    user_data["xp"] = xp
    user_data["level"] = level_from_xp(xp)
    guardar_xp()
    await ctx.send(f"✅ XP de {usuario.mention} establecido a {xp:,} (Nivel {user_data['level']}).")


@bot.command(name="set-level", help="Establece el nivel de un usuario. Uso: .set-level (@usuario) (nivel) o responde al usuario")
@commands.has_permissions(manage_guild=True)
async def prefix_set_level(ctx, *, args: str = ""):
    """Establece el nivel de un usuario manualmente. Respondiendo: `.set-level (nivel)`."""
    tokens = args.split()
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)
    if usuario_repl is not None:
        if not tokens:
            return await ctx.send("❌ Debes indicar el nivel. Uso: `.set-level (nivel)` respondiendo al usuario.")
        usuario = miembro_repl or usuario_repl
        try:
            nivel = int(tokens[0])
        except ValueError:
            return await ctx.send("❌ El nivel debe ser un número entero.")
    else:
        if len(tokens) < 2:
            return await ctx.send("❌ Uso correcto: `.set-level (@usuario) (nivel)` o responde al usuario.")
        usuario, _, err = await resolver_usuario(ctx.guild, tokens[0])
        if err:
            return await ctx.send(err)
        try:
            nivel = int(tokens[1])
        except ValueError:
            return await ctx.send("❌ El nivel debe ser un número entero.")
    if nivel < 0:
        return await ctx.send("❌ El nivel no puede ser negativo.")
    if usuario.bot:
        return await ctx.send("❌ No se puede modificar nivel de bots.")
    user_data = get_user_xp(ctx.guild.id, usuario.id)
    user_data["level"] = nivel
    user_data["xp"] = xp_for_level(nivel)
    guardar_xp()
    await ctx.send(f"✅ Nivel de {usuario.mention} establecido a {nivel} (XP: {user_data['xp']:,}).")


@bot.command(name="add-xp", help="Añade XP a un usuario. Uso: .add-xp (@usuario) (cantidad) o responde al usuario")
@commands.has_permissions(manage_guild=True)
async def prefix_add_xp(ctx, *, args: str = ""):
    """Añade XP a un usuario. Respondiendo: `.add-xp (cantidad)`."""
    tokens = args.split()
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)
    if usuario_repl is not None:
        if not tokens:
            return await ctx.send("❌ Debes indicar la cantidad. Uso: `.add-xp (cantidad)` respondiendo al usuario.")
        usuario = miembro_repl or usuario_repl
        try:
            cantidad = int(tokens[0])
        except ValueError:
            return await ctx.send("❌ La cantidad debe ser un número entero.")
    else:
        if len(tokens) < 2:
            return await ctx.send("❌ Uso correcto: `.add-xp (@usuario) (cantidad)` o responde al usuario.")
        usuario, _, err = await resolver_usuario(ctx.guild, tokens[0])
        if err:
            return await ctx.send(err)
        try:
            cantidad = int(tokens[1])
        except ValueError:
            return await ctx.send("❌ La cantidad debe ser un número entero.")
    if cantidad <= 0:
        return await ctx.send("❌ La cantidad debe ser positiva.")
    if usuario.bot:
        return await ctx.send("❌ No se puede añadir XP a bots.")
    user_data = get_user_xp(ctx.guild.id, usuario.id)
    old_level = user_data["level"]
    user_data["xp"] += cantidad
    new_level = level_from_xp(user_data["xp"])
    user_data["level"] = new_level
    guardar_xp()
    msg = f"✅ Añadidos {cantidad:,} XP a {usuario.mention}. Total: {user_data['xp']:,} (Nivel {new_level})"
    if new_level > old_level:
        msg += f"\n🎉 ¡Subió de nivel {old_level} a {new_level}!"
    await ctx.send(msg)


@bot.command(name="remove-xp", help="Quita XP a un usuario. Uso: .remove-xp (@usuario) (cantidad) o responde al usuario")
@commands.has_permissions(manage_guild=True)
async def prefix_remove_xp(ctx, *, args: str = ""):
    """Quita XP a un usuario. Respondiendo: `.remove-xp (cantidad)`."""
    tokens = args.split()
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)
    if usuario_repl is not None:
        if not tokens:
            return await ctx.send("❌ Debes indicar la cantidad. Uso: `.remove-xp (cantidad)` respondiendo al usuario.")
        usuario = miembro_repl or usuario_repl
        try:
            cantidad = int(tokens[0])
        except ValueError:
            return await ctx.send("❌ La cantidad debe ser un número entero.")
    else:
        if len(tokens) < 2:
            return await ctx.send("❌ Uso correcto: `.remove-xp (@usuario) (cantidad)` o responde al usuario.")
        usuario, _, err = await resolver_usuario(ctx.guild, tokens[0])
        if err:
            return await ctx.send(err)
        try:
            cantidad = int(tokens[1])
        except ValueError:
            return await ctx.send("❌ La cantidad debe ser un número entero.")
    if cantidad <= 0:
        return await ctx.send("❌ La cantidad debe ser positiva.")
    if usuario.bot:
        return await ctx.send("❌ No se puede quitar XP a bots.")
    user_data = get_user_xp(ctx.guild.id, usuario.id)
    user_data["xp"] = max(0, user_data["xp"] - cantidad)
    user_data["level"] = level_from_xp(user_data["xp"])
    guardar_xp()
    await ctx.send(f"✅ Quitados {cantidad:,} XP a {usuario.mention}. Total: {user_data['xp']:,} (Nivel {user_data['level']})")


@bot.command(name="reset-level", help="Reinicia el XP y nivel de un usuario. Uso: .reset-level (@usuario) o responde al usuario")
@commands.has_permissions(manage_guild=True)
async def prefix_reset_level(ctx, *, args: str = ""):
    """Reinicia el XP y nivel de un usuario. Puedes responder a su mensaje y usar `.reset-level`."""
    tokens = args.split()
    usuario_repl, miembro_repl = await resolver_objetivo_replica(ctx)
    if usuario_repl is not None:
        if tokens:
            return await ctx.send("❌ `.reset-level` no necesita argumentos al responder a un usuario.")
        usuario = miembro_repl or usuario_repl
    else:
        if not tokens:
            return await ctx.send("❌ Uso correcto: `.reset-level (@usuario)` o responde al usuario.")
        usuario, _, err = await resolver_usuario(ctx.guild, tokens[0])
        if err:
            return await ctx.send(err)
    if usuario.bot:
        return await ctx.send("❌ No se puede reiniciar nivel de bots.")
    gid = str(ctx.guild.id)
    uid = str(usuario.id)
    if gid in xp_db and uid in xp_db[gid]:
        del xp_db[gid][uid]
        guardar_xp()
        await ctx.send(f"✅ XP y nivel de {usuario.mention} reiniciados.")
    else:
        await ctx.send("ℹ️ Ese usuario no tiene datos de XP.")


# ===================== STARBOARD PREFIX COMMANDS =====================

@bot.command(name="starboard", help="Muestra la configuración actual del starboard")
async def starboard_config_cmd(ctx):
    gid = str(ctx.guild.id)
    config = starboard_db.get(gid, {})
    enabled = config.get("enabled", False)
    channel_id = config.get("channel_id")
    threshold = config.get("threshold", 5)
    canal = ctx.guild.get_channel(channel_id) if channel_id else None
    embed = discord.Embed(title="⭐ Configuración del Starboard", color=discord.Color.gold())
    embed.add_field(name="Estado", value="✅ Activado" if enabled else "❌ Desactivado", inline=True)
    embed.add_field(name="Canal", value=canal.mention if canal else "No establecido", inline=True)
    embed.add_field(name="Reacciones necesarias", value=str(threshold), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="starreactions", aliases=["starreacciones"], help="Cambia la cantidad de estrellas necesarias. Uso: .starreactions <número>")
@commands.has_permissions(manage_guild=True)
async def starreactions_cmd(ctx, cantidad: int):
    if cantidad < 1:
        return await ctx.send("❌ El número debe ser al menos 1.")
    gid = str(ctx.guild.id)
    config = starboard_db.setdefault(gid, {"enabled": False, "channel_id": None, "threshold": 5, "posted": {}})
    config["threshold"] = cantidad
    guardar_starboard()
    await ctx.send(f"✅ Estrellas requeridas establecidas a **{cantidad}**.")


class StarboardChannelSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild):
        options = [
            discord.SelectOption(label=c.name, value=str(c.id), description=f"ID: {c.id}")
            for c in guild.text_channels
        ][:25]  # Discord limit 25 options
        super().__init__(placeholder="Selecciona el canal para el starboard...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.view.author:
            return await interaction.response.send_message("❌ Solo quien ejecutó el comando puede seleccionar.", ephemeral=True)
        channel_id = int(self.values[0])
        gid = str(interaction.guild.id)
        config = starboard_db.setdefault(gid, {"enabled": False, "channel_id": None, "threshold": 5, "posted": {}})
        config["channel_id"] = channel_id
        config["enabled"] = True
        guardar_starboard()
        channel = interaction.guild.get_channel(channel_id)
        await interaction.response.edit_message(content=f"✅ Starboard activado en {channel.mention}.", embed=None, view=None)


class StarboardEnableView(discord.ui.View):
    def __init__(self, author: discord.User, guild: discord.Guild):
        super().__init__(timeout=60)
        self.author = author
        self.add_item(StarboardChannelSelect(guild))


@bot.command(name="starenable", help="Activa el starboard y pide elegir canal")
@commands.has_permissions(manage_guild=True)
async def starenable_cmd(ctx):
    if not ctx.guild.text_channels:
        return await ctx.send("❌ No hay canales de texto en este servidor.")
    view = StarboardEnableView(ctx.author, ctx.guild)
    await ctx.send("Selecciona el canal donde se enviarán los mensajes estrella:", view=view)


@bot.command(name="stardisable", help="Desactiva el starboard")
@commands.has_permissions(manage_guild=True)
async def stardisable_cmd(ctx):
    gid = str(ctx.guild.id)
    if gid in starboard_db:
        starboard_db[gid]["enabled"] = False
        guardar_starboard()
    await ctx.send("✅ Starboard desactivado.")


# ============================================================
#  SLASH: /level (Sistema de Niveles/XP)
# ============================================================

level_group = app_commands.Group(name="level", description="Sistema de niveles y XP")


@level_group.command(name="rank", description="Muestra el rango y nivel de un usuario")
@app_commands.describe(usuario="Usuario a consultar (opcional, por defecto tú)")
async def slash_rank(interaction: discord.Interaction, usuario: discord.Member = None):
    target = usuario or interaction.user
    if target.bot:
        return await interaction.response.send_message("❌ Los bots no tienen nivel.", ephemeral=True)
    gid = str(interaction.guild.id)
    uid = str(target.id)
    user_data = get_user_xp(interaction.guild.id, target.id)
    xp_in_level, xp_needed, progress = get_xp_progress(user_data)
    rank = get_user_rank(interaction.guild.id, target.id)
    total_users = len(get_guild_leaderboard(interaction.guild.id))
    
    xp_current = user_data["xp"]
    xp_total_next = xp_for_level(user_data["level"] + 1)
    xp_current_level = xp_for_level(user_data["level"])
    
    bar = create_progress_bar(progress, 20)
    
    embed = discord.Embed(title=f"📊 Rango de {target.display_name}", color=target.color or discord.Color.blurple())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="📈 Nivel", value=f"**{user_data['level']}**", inline=True)
    embed.add_field(name="⭐ XP Total", value=f"**{user_data['xp']:,}**", inline=True)
    embed.add_field(name="🏆 Posición", value=f"**#{rank}** de {total_users}", inline=True)
    embed.add_field(name="", value="", inline=False)
    embed.add_field(
        name=f"Progreso hacia nivel {user_data['level'] + 1}",
        value=f"`{bar}` {progress:.1f}%\n**{xp_in_level:,} / {xp_needed:,} XP**",
        inline=False
    )
    embed.set_footer(text=f"XP en este nivel: {xp_in_level:,} / {xp_needed:,} | XP total para siguiente nivel: {xp_total_next:,}")
    await interaction.response.send_message(embed=embed)


@level_group.command(name="levels", description="Muestra información del sistema de niveles")
@app_commands.describe(usuario="Usuario a consultar (opcional, por defecto tú)")
async def slash_levels(interaction: discord.Interaction, usuario: discord.Member = None):
    target = usuario or interaction.user
    if target.bot:
        return await interaction.response.send_message("❌ Los bots no tienen nivel.", ephemeral=True)
    user_data = get_user_xp(interaction.guild.id, target.id)
    xp_in_level, xp_needed, progress = get_xp_progress(user_data)
    rank = get_user_rank(interaction.guild.id, target.id)
    total_users = len(get_guild_leaderboard(interaction.guild.id))
    
    embed = discord.Embed(title=f"📊 Nivel de {target.display_name}", color=target.color or discord.Color.blurple())
    embed.add_field(name="Nivel actual", value=f"**{user_data['level']}**", inline=True)
    embed.add_field(name="XP actual", value=f"**{user_data['xp']:,}**", inline=True)
    embed.add_field(name="XP para siguiente nivel", value=f"**{xp_needed:,}**", inline=True)
    embed.add_field(name="Progreso", value=f"{progress:.1f}%", inline=True)
    embed.add_field(name="Posición en ranking", value=f"#{rank} de {total_users}", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


class LeaderboardView(discord.ui.View):
    def __init__(self, guild: discord.Guild, leaderboard: list, per_page: int = 10):
        super().__init__(timeout=60)
        self.guild = guild
        self.leaderboard = leaderboard
        self.per_page = per_page
        self.current_page = 0
        self.max_page = (len(leaderboard) - 1) // per_page
        self.update_buttons()

    def update_buttons(self):
        self.prev.disabled = self.current_page == 0
        self.next.disabled = self.current_page >= self.max_page

    def get_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_data = self.leaderboard[start:end]
        
        embed = discord.Embed(
            title=f"🏆 Ranking de {self.guild.name}",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        if not page_data:
            embed.description = "No hay datos."
            return embed
        
        lines = []
        for i, (uid, xp, level) in enumerate(page_data, start + 1):
            member = self.guild.get_member(uid)
            name = member.display_name if member else f"Usuario desconocido ({uid})"
            lines.append(f"`#{i}` {name} — **Nivel {level}** — `{xp:,} XP`")
        
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Página {self.current_page + 1} / {self.max_page + 1} • {len(self.leaderboard)} usuarios")
        return embed

    @discord.ui.button(label="◀️ Anterior", style=discord.ButtonStyle.secondary, custom_id="lb_prev")
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Siguiente ▶️", style=discord.ButtonStyle.secondary, custom_id="lb_next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.max_page:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_embed(), view=self)


@level_group.command(name="leaderboard", description="Muestra el ranking de usuarios con más XP")
@app_commands.describe(pagina="Página inicial (opcional)")
async def slash_leaderboard(interaction: discord.Interaction, pagina: int = 1):
    leaderboard = get_guild_leaderboard(interaction.guild.id)
    if not leaderboard:
        return await interaction.response.send_message("ℹ️ No hay datos de XP en este servidor.", ephemeral=True)
    
    view = LeaderboardView(interaction.guild, leaderboard)
    pagina = max(1, min(pagina, view.max_page + 1))
    view.current_page = pagina - 1
    view.update_buttons()
    await interaction.response.send_message(embed=view.get_embed(), view=view)


# ============================================================
#  SLASH: /level-admin (Administración del sistema de niveles)
# ============================================================

level_admin_group = app_commands.Group(name="level-admin", description="Administración del sistema de niveles")


@level_admin_group.command(name="config", description="Configura el sistema de niveles")
@app_commands.describe(
    enabled="Activar/desactivar sistema",
    xp_min="XP mínimo por mensaje",
    xp_max="XP máximo por mensaje",
    cooldown="Cooldown en segundos",
    canal="Canal para anuncios de nivel",
    mensaje="Mensaje de level up (usa {user}, {level}, {xp}, {server})",
    anuncios="Activar/desactivar anuncios de level up"
)
@app_commands.default_permissions(manage_guild=True)
async def slash_level_config(
    interaction: discord.Interaction,
    enabled: bool = None,
    xp_min: int = None,
    xp_max: int = None,
    cooldown: int = None,
    canal: discord.TextChannel = None,
    mensaje: str = None,
    anuncios: bool = None
):
    gid = str(interaction.guild.id)
    config = get_xp_config(interaction.guild.id)
    
    if enabled is not None:
        config["enabled"] = enabled
    if xp_min is not None:
        if xp_min < 0:
            return await interaction.response.send_message("❌ El XP mínimo no puede ser negativo.", ephemeral=True)
        config["xp_min"] = xp_min
    if xp_max is not None:
        if xp_max < 0:
            return await interaction.response.send_message("❌ El XP máximo no puede ser negativo.", ephemeral=True)
        config["xp_max"] = xp_max
    if xp_min is not None and xp_max is not None and config["xp_min"] > config["xp_max"]:
        return await interaction.response.send_message("❌ El XP mínimo no puede ser mayor que el máximo.", ephemeral=True)
    if cooldown is not None:
        if cooldown < 0:
            return await interaction.response.send_message("❌ El cooldown no puede ser negativo.", ephemeral=True)
        config["cooldown"] = cooldown
    if canal is not None:
        config["levelup_channel"] = canal.id
    if mensaje is not None:
        config["levelup_msg"] = mensaje
    if anuncios is not None:
        config["levelup_enabled"] = anuncios
    
    xp_config_db[str(interaction.guild.id)] = config
    guardar_xp()
    
    embed = discord.Embed(title="⚙️ Configuración de niveles actualizada", color=discord.Color.green())
    embed.add_field(name="Estado", value="✅ Activado" if config["enabled"] else "❌ Desactivado", inline=True)
    embed.add_field(name="XP por mensaje", value=f"{config['xp_min']} - {config['xp_max']}", inline=True)
    embed.add_field(name="Cooldown", value=f"{config['cooldown']}s", inline=True)
    embed.add_field(name="Canal anuncios", value=f"<#{config['levelup_channel']}>" if config["levelup_channel"] else "Canal actual", inline=True)
    embed.add_field(name="Anuncios", value="✅ Activados" if config["levelup_enabled"] else "❌ Desactivados", inline=True)
    if config["levelup_msg"]:
        embed.add_field(name="Mensaje level up", value=config["levelup_msg"], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@level_admin_group.command(name="set-role", description="Asigna un rol al alcanzar un nivel")
@app_commands.describe(nivel="Nivel requerido", rol="Rol a otorgar")
@app_commands.default_permissions(manage_guild=True)
async def slash_set_level_role(interaction: discord.Interaction, nivel: int, rol: discord.Role):
    if nivel < 1:
        return await interaction.response.send_message("❌ El nivel debe ser al menos 1.", ephemeral=True)
    if rol.position >= interaction.guild.me.top_role.position:
        return await interaction.response.send_message("❌ No puedo asignar ese rol (está por encima de mi rol).", ephemeral=True)
    gid = str(interaction.guild.id)
    level_roles_db.setdefault(gid, {})
    level_roles_db[gid][str(nivel)] = str(rol.id)
    guardar_level_roles()
    await interaction.response.send_message(f"✅ Rol {rol.mention} asignado para el nivel {nivel}.", ephemeral=True)


@level_admin_group.command(name="remove-role", description="Elimina una recompensa de rol por nivel")
@app_commands.describe(nivel="Nivel del que quitar la recompensa")
@app_commands.default_permissions(manage_guild=True)
async def slash_remove_level_role(interaction: discord.Interaction, nivel: int):
    gid = str(interaction.guild.id)
    if gid not in level_roles_db or str(nivel) not in level_roles_db[gid]:
        return await interaction.response.send_message("❌ No hay recompensa configurada para ese nivel.", ephemeral=True)
    del level_roles_db[gid][str(nivel)]
    if not level_roles_db[gid]:
        del level_roles_db[gid]
    guardar_level_roles()
    await interaction.response.send_message(f"✅ Recompensa de nivel {nivel} eliminada.", ephemeral=True)


@level_admin_group.command(name="set-xp", description="Establece el XP de un usuario manualmente")
@app_commands.describe(usuario="Usuario", xp="Cantidad de XP")
@app_commands.default_permissions(manage_guild=True)
async def slash_set_xp(interaction: discord.Interaction, usuario: discord.Member, xp: int):
    if xp < 0:
        return await interaction.response.send_message("❌ El XP no puede ser negativo.", ephemeral=True)
    if usuario.bot:
        return await interaction.response.send_message("❌ No se puede modificar XP de bots.", ephemeral=True)
    user_data = get_user_xp(interaction.guild.id, usuario.id)
    user_data["xp"] = xp
    user_data["level"] = level_from_xp(xp)
    guardar_xp()
    await interaction.response.send_message(f"✅ XP de {usuario.mention} establecido a {xp:,} (Nivel {user_data['level']}).", ephemeral=True)


@level_admin_group.command(name="set-level", description="Establece el nivel de un usuario manualmente")
@app_commands.describe(usuario="Usuario", nivel="Nivel a establecer")
@app_commands.default_permissions(manage_guild=True)
async def slash_set_level(interaction: discord.Interaction, usuario: discord.Member, nivel: int):
    if nivel < 0:
        return await interaction.response.send_message("❌ El nivel no puede ser negativo.", ephemeral=True)
    if usuario.bot:
        return await interaction.response.send_message("❌ No se puede modificar nivel de bots.", ephemeral=True)
    user_data = get_user_xp(interaction.guild.id, usuario.id)
    user_data["level"] = nivel
    user_data["xp"] = xp_for_level(nivel)
    guardar_xp()
    await interaction.response.send_message(f"✅ Nivel de {usuario.mention} establecido a {nivel} (XP: {user_data['xp']:,}).", ephemeral=True)


@level_admin_group.command(name="add-xp", description="Añade XP a un usuario")
@app_commands.describe(usuario="Usuario", cantidad="Cantidad de XP a añadir")
@app_commands.default_permissions(manage_guild=True)
async def slash_add_xp(interaction: discord.Interaction, usuario: discord.Member, cantidad: int):
    if cantidad <= 0:
        return await interaction.response.send_message("❌ La cantidad debe ser positiva.", ephemeral=True)
    if usuario.bot:
        return await interaction.response.send_message("❌ No se puede añadir XP a bots.", ephemeral=True)
    user_data = get_user_xp(interaction.guild.id, usuario.id)
    old_level = user_data["level"]
    user_data["xp"] += cantidad
    new_level = level_from_xp(user_data["xp"])
    user_data["level"] = new_level
    guardar_xp()
    msg = f"✅ Añadidos {cantidad:,} XP a {usuario.mention}. Total: {user_data['xp']:,} (Nivel {new_level})"
    if new_level > old_level:
        msg += f"\n🎉 ¡Subió de nivel {old_level} a {new_level}!"
    await interaction.response.send_message(msg, ephemeral=True)


@level_admin_group.command(name="remove-xp", description="Quita XP a un usuario")
@app_commands.describe(usuario="Usuario", cantidad="Cantidad de XP a quitar")
@app_commands.default_permissions(manage_guild=True)
async def slash_remove_xp(interaction: discord.Interaction, usuario: discord.Member, cantidad: int):
    if cantidad <= 0:
        return await interaction.response.send_message("❌ La cantidad debe ser positiva.", ephemeral=True)
    if usuario.bot:
        return await interaction.response.send_message("❌ No se puede quitar XP a bots.", ephemeral=True)
    user_data = get_user_xp(interaction.guild.id, usuario.id)
    user_data["xp"] = max(0, user_data["xp"] - cantidad)
    user_data["level"] = level_from_xp(user_data["xp"])
    guardar_xp()
    await interaction.response.send_message(f"✅ Quitados {cantidad:,} XP a {usuario.mention}. Total: {user_data['xp']:,} (Nivel {user_data['level']})", ephemeral=True)


@level_admin_group.command(name="reset", description="Reinicia el XP y nivel de un usuario")
@app_commands.describe(usuario="Usuario a reiniciar")
@app_commands.default_permissions(manage_guild=True)
async def slash_reset_level(interaction: discord.Interaction, usuario: discord.Member):
    if usuario.bot:
        return await interaction.response.send_message("❌ No se puede reiniciar nivel de bots.", ephemeral=True)
    gid = str(interaction.guild.id)
    uid = str(usuario.id)
    if gid in xp_db and uid in xp_db[gid]:
        del xp_db[gid][uid]
        guardar_xp()
        await interaction.response.send_message(f"✅ XP y nivel de {usuario.mention} reiniciados.", ephemeral=True)
    else:
        await interaction.response.send_message("ℹ️ Ese usuario no tiene datos de XP.", ephemeral=True)


# --- JUMBO slash ---
@bot.tree.command(name="jumbo", description="Aumenta un emoji (personalizado o Unicode)")
@app_commands.describe(emoji="Emoji a agrandar (personalizado o Unicode)")
async def slash_jumbo(interaction: discord.Interaction, emoji: str):
    custom = discord.PartialEmoji.from_str(emoji)
    if custom.id:
        url = custom.url
        name = custom.name
        embed = discord.Embed(title=f"Jumbo: {name}", color=discord.Color.blurple())
        embed.set_image(url=url)
        return await interaction.response.send_message(embed=embed)
    codepoints = "-".join(f"{ord(c):x}" for c in emoji)
    url = f"https://twemoji.maxcdn.com/v/latest/72x72/{codepoints}.png"
    embed = discord.Embed(title="Jumbo", color=discord.Color.blurple())
    embed.set_image(url=url)
    await interaction.response.send_message(embed=embed)


# ============================================================
#  SLASH: /starboard
# ============================================================

starboard_group = app_commands.Group(name="starboard", description="Configuración del starboard")


@starboard_group.command(name="config", description="Muestra la configuración actual del starboard")
@app_commands.default_permissions(manage_guild=True)
async def slash_starboard_config(interaction: discord.Interaction):
    gid = str(interaction.guild.id)
    config = starboard_db.get(gid, {})
    enabled = config.get("enabled", False)
    channel_id = config.get("channel_id")
    threshold = config.get("threshold", 5)
    canal = interaction.guild.get_channel(channel_id) if channel_id else None
    embed = discord.Embed(title="⭐ Configuración del Starboard", color=discord.Color.gold())
    embed.add_field(name="Estado", value="✅ Activado" if enabled else "❌ Desactivado", inline=True)
    embed.add_field(name="Canal", value=canal.mention if canal else "No establecido", inline=True)
    embed.add_field(name="Reacciones necesarias", value=str(threshold), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@starboard_group.command(name="reactions", description="Cambia la cantidad de estrellas necesarias")
@app_commands.describe(cantidad="Número de ⭐ requeridas (mínimo 1)")
@app_commands.default_permissions(manage_guild=True)
async def slash_starboard_reactions(interaction: discord.Interaction, cantidad: int):
    if cantidad < 1:
        return await interaction.response.send_message("❌ El número debe ser al menos 1.", ephemeral=True)
    gid = str(interaction.guild.id)
    config = starboard_db.setdefault(gid, {"enabled": False, "channel_id": None, "threshold": 5, "posted": {}})
    config["threshold"] = cantidad
    guardar_starboard()
    await interaction.response.send_message(f"✅ Estrellas requeridas establecidas a **{cantidad}**.", ephemeral=True)


@starboard_group.command(name="enable", description="Activa el starboard en un canal")
@app_commands.describe(canal="Canal donde se enviarán los mensajes estrella")
@app_commands.default_permissions(manage_guild=True)
async def slash_starboard_enable(interaction: discord.Interaction, canal: discord.TextChannel):
    gid = str(interaction.guild.id)
    config = starboard_db.setdefault(gid, {"enabled": False, "channel_id": None, "threshold": 5, "posted": {}})
    config["channel_id"] = canal.id
    config["enabled"] = True
    guardar_starboard()
    await interaction.response.send_message(f"✅ Starboard activado en {canal.mention}.", ephemeral=True)


@starboard_group.command(name="disable", description="Desactiva el starboard")
@app_commands.default_permissions(manage_guild=True)
async def slash_starboard_disable(interaction: discord.Interaction):
    gid = str(interaction.guild.id)
    if gid in starboard_db:
        starboard_db[gid]["enabled"] = False
        guardar_starboard()
    await interaction.response.send_message("✅ Starboard desactivado.", ephemeral=True)


bot.tree.add_command(level_group)
bot.tree.add_command(level_admin_group)
bot.tree.add_command(starboard_group)


# ============================================================
# 💰 SISTEMA DE ECONOMÍA (estilo UnbelievaBoat)
# ============================================================

ECONOMY_PATH = "economy.json"
economy_db = {}      # guild_id -> {user_id: {cash, bank, inventory, ...}}
econ_config_db = {}  # guild_id -> configuración de economía
SHOP_PATH = "economy_shop.json"
shop_db = {}         # guild_id -> {item: {"price": int, "description": str}}

ECONOMY_COOLDOWNS = {
    "daily": 24 * 3600,
    "weekly": 7 * 24 * 3600,
    "monthly": 30 * 24 * 3600,
    "work": 3600,
    "crime": 45 * 60,
    "slut": 45 * 60,
    "rob": 3600,
}

WORK_MENSAJES = [
    "🍕 Repartiste pizzas toda la tarde",
    "☕ Sobreviviste a un turno de camarero",
    "📦 Vendiste cosas por internet",
    "🚗 Hiciste algunos viajes de taxi",
    "🎮 Hiciste un stream y te donaron",
    "🐶 Paseaste los perros del vecindario",
    "🍔 Freiste hamburguesas sin quemarte",
    "🧹 Limpiaste el garaje de tu abuela",
]

CRIMEN_EXITOS = [
    "🦹 Hackeaste una máquina de vending",
    "🕵️ Vendiste gafas de sol muy sospechosas",
    "💳 Clonaste la tarjeta de fidelidad de un supermercado",
    "📱 Flipaste un teléfono que encontraste",
    "💻 Estafaste a un príncipe nigeriano (pobre)",
]

SLUT_MENSAJES = [
    "💋 Hiciste trabajos extraños. Nadie pregunta",
    "🌹 Vendiste besos virtuales por NFT",
    "🎭 Hiciste un favor que no se comenta en la cena familiar",
    "💅 Cobraste por decir piropos en el chat",
]


def cargar_economy():
    global economy_db, econ_config_db
    if os.path.exists(ECONOMY_PATH):
        try:
            with open(ECONOMY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            economy_db = data.get("users", {})
            econ_config_db = data.get("config", {})
        except (json.JSONDecodeError, OSError):
            economy_db = {}
            econ_config_db = {}
    else:
        economy_db = {}
        econ_config_db = {}
    print(f"Economía cargada: {sum(len(v) for v in economy_db.values())} usuarios en {len(economy_db)} servidores.")


def guardar_economy():
    try:
        with open(ECONOMY_PATH, "w", encoding="utf-8") as f:
            json.dump({"users": economy_db, "config": econ_config_db}, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando economy.json: {e}")


def cargar_shop():
    global shop_db
    if os.path.exists(SHOP_PATH):
        try:
            with open(SHOP_PATH, "r", encoding="utf-8") as f:
                shop_db = json.load(f)
        except (json.JSONDecodeError, OSError):
            shop_db = {}
    else:
        shop_db = {}
    print(f"Tienda cargada: {sum(len(v) for v in shop_db.values())} items en {len(shop_db)} servidores.")


def guardar_shop():
    try:
        with open(SHOP_PATH, "w", encoding="utf-8") as f:
            json.dump(shop_db, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error guardando economy_shop.json: {e}")


def get_econ_config(guild_id):
    gid = str(guild_id)
    defaults = {
        "currency": "$",
        "start_balance": 0,
        "daily_min": 200, "daily_max": 400,
        "weekly_min": 1000, "weekly_max": 2000,
        "monthly_min": 4000, "monthly_max": 8000,
        "work_min": 50, "work_max": 200,
        "crime_min": 100, "crime_max": 500, "crime_fallo": 0.35,
        "slut_min": 150, "slut_max": 400, "slut_fallo": 0.15,
        "rob_min": 0.10, "rob_max": 0.25, "rob_fallo": 0.40,
    }
    if gid not in econ_config_db:
        econ_config_db[gid] = defaults.copy()
    else:
        for k, v in defaults.items():
            econ_config_db[gid].setdefault(k, v)
    return econ_config_db[gid]


def get_user_econ(guild_id, user_id):
    gid, uid = str(guild_id), str(user_id)
    cfg = get_econ_config(gid)
    gdata = economy_db.setdefault(gid, {})
    if uid not in gdata:
        gdata[uid] = {
            "cash": cfg["start_balance"], "bank": 0, "inventory": {}, "jailed_until": 0,
            "last_daily": 0, "last_weekly": 0, "last_monthly": 0,
            "last_work": 0, "last_crime": 0, "last_slut": 0, "last_rob": 0,
        }
    u = gdata[uid]
    u.setdefault("inventory", {})
    return u


def fmt_dinero(n, cfg):
    return f"{cfg['currency']}{n:,}"


def econ_fmt_segundos(seg):
    h = seg // 3600
    m = (seg % 3600) // 60
    s = seg % 60
    partes = []
    if h: partes.append(f"{h}h")
    if m: partes.append(f"{m}m")
    if s: partes.append(f"{s}s")
    return " ".join(partes) or "0s"


def econ_carcel_restante(u):
    rest = u.get("jailed_until", 0) - time.time()
    return int(rest) if rest > 0 else 0


def econ_check_carcel(u, comando="este comando"):
    rest = econ_carcel_restante(u)
    if rest > 0:
        return f"🚔 Estás en la cárcel. Podrás usar {comando} en {econ_fmt_segundos(rest)}."
    return None


def econ_cooldown(u, clave, segundos):
    ahora = time.time()
    rest = int(u.get(f"last_{clave}", 0) + segundos - ahora)
    if rest > 0:
        return False, rest
    u[f"last_{clave}"] = ahora
    return True, 0


def parse_monto(texto, total):
    t = (texto or "").strip().lower()
    if not t:
        return None, "Debes indicar un monto (número, `all` o `mitad`)."
    if t in ("all", "todo", "max"):
        monto = total
    elif t in ("half", "mitad"):
        monto = total // 2
    else:
        t2 = t.replace(",", "").replace("$", "")
        if not t2.isdigit():
            return None, f"`{texto}` no es un monto válido."
        monto = int(t2)
    if monto <= 0:
        return None, "El monto debe ser mayor que 0."
    if monto > total:
        return None, f"No tienes suficiente dinero (máximo disponible: {total:,})."
    return monto, None


def _parse_entero(texto):
    t = (texto or "").strip().replace(",", "")
    if not t.isdigit():
        return None
    return int(t)


def _mk_resp(content=None, embed=None):
    kw = {}
    if content:
        kw["content"] = content
    if embed is not None:
        kw["embed"] = embed
    return kw


def _eco_balance(guild, miembro):
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, miembro.id)
    total = u["cash"] + u["bank"]
    e = discord.Embed(title=f"💰 Balance de {miembro.display_name}", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
    e.set_thumbnail(url=miembro.display_avatar.url)
    e.add_field(name="💵 Efectivo", value=fmt_dinero(u["cash"], cfg), inline=True)
    e.add_field(name="🏦 Banco", value=fmt_dinero(u["bank"], cfg), inline=True)
    e.add_field(name="💼 Total", value=fmt_dinero(total, cfg), inline=True)
    carcel = econ_carcel_restante(u)
    if carcel:
        e.add_field(name="🚔 Estado", value=f"En la cárcel ({econ_fmt_segundos(carcel)} restantes)", inline=False)
    return _mk_resp(embed=e)


def _eco_periodico(guild, author, clave):
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, author.id)
    ok, rest = econ_cooldown(u, clave, ECONOMY_COOLDOWNS[clave])
    if not ok:
        return _mk_resp(f"⏳ Ya reclamaste tu recompensa {clave}. Vuelve en {econ_fmt_segundos(rest)}.")
    monto = random.randint(cfg[f"{clave}_min"], cfg[f"{clave}_max"])
    u["cash"] += monto
    guardar_economy()
    e = discord.Embed(title=f"🎁 Recompensa {clave} reclamada", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    e.set_thumbnail(url=author.display_avatar.url)
    e.add_field(name="Ganancia", value=fmt_dinero(monto, cfg), inline=True)
    e.add_field(name="Efectivo", value=fmt_dinero(u["cash"], cfg), inline=True)
    return _mk_resp(embed=e)


def _eco_work(guild, author):
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, author.id)
    carcel = econ_check_carcel(u, "work")
    if carcel:
        return _mk_resp(carcel)
    ok, rest = econ_cooldown(u, "work", ECONOMY_COOLDOWNS["work"])
    if not ok:
        return _mk_resp(f"⏳ Estás agotado. Vuelve al trabajo en {econ_fmt_segundos(rest)}.")
    monto = random.randint(cfg["work_min"], cfg["work_max"])
    u["cash"] += monto
    guardar_economy()
    e = discord.Embed(title="💼 Jornada completada", description=random.choice(WORK_MENSAJES), color=discord.Color.green(), timestamp=discord.utils.utcnow())
    e.add_field(name="Salario", value=fmt_dinero(monto, cfg), inline=True)
    e.add_field(name="Efectivo", value=fmt_dinero(u["cash"], cfg), inline=True)
    return _mk_resp(embed=e)


def _eco_crime(guild, author):
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, author.id)
    carcel = econ_check_carcel(u, "crime")
    if carcel:
        return _mk_resp(carcel)
    ok, rest = econ_cooldown(u, "crime", ECONOMY_COOLDOWNS["crime"])
    if not ok:
        return _mk_resp(f"⏳ Debes mantener un perfil bajo. Vuelve a delinquir en {econ_fmt_segundos(rest)}.")
    if random.random() < cfg["crime_fallo"]:
        carcel_seg = random.randint(30, 90) * 60
        u["jailed_until"] = time.time() + carcel_seg
        multa = min(u["cash"], random.randint(50, 200))
        u["cash"] -= multa
        guardar_economy()
        e = discord.Embed(title="🚔 Te pillaron", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow())
        e.add_field(name="Condena", value=f"Cárcel {econ_fmt_segundos(carcel_seg)}", inline=True)
        e.add_field(name="Multa", value=fmt_dinero(multa, cfg), inline=True)
    else:
        monto = random.randint(cfg["crime_min"], cfg["crime_max"])
        u["cash"] += monto
        guardar_economy()
        e = discord.Embed(title="🦹 Golpe exitoso", description=random.choice(CRIMEN_EXITOS), color=discord.Color.dark_green(), timestamp=discord.utils.utcnow())
        e.add_field(name="Botín", value=fmt_dinero(monto, cfg), inline=True)
        e.add_field(name="Efectivo", value=fmt_dinero(u["cash"], cfg), inline=True)
    return _mk_resp(embed=e)


def _eco_slut(guild, author):
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, author.id)
    carcel = econ_check_carcel(u, "slut")
    if carcel:
        return _mk_resp(carcel)
    ok, rest = econ_cooldown(u, "slut", ECONOMY_COOLDOWNS["slut"])
    if not ok:
        return _mk_resp(f"⏳ Necesitas descansar. Vuelve en {econ_fmt_segundos(rest)}.")
    if random.random() < cfg["slut_fallo"]:
        perdida = min(u["cash"], random.randint(50, 150))
        u["cash"] -= perdida
        guardar_economy()
        e = discord.Embed(title="💀 Salió mal el negocio", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow())
        e.add_field(name="Perdiste", value=fmt_dinero(perdida, cfg), inline=True)
    else:
        monto = random.randint(cfg["slut_min"], cfg["slut_max"])
        u["cash"] += monto
        guardar_economy()
        e = discord.Embed(title="💋 Negocio cerrado", description=random.choice(SLUT_MENSAJES), color=discord.Color.magenta(), timestamp=discord.utils.utcnow())
        e.add_field(name="Ganancia", value=fmt_dinero(monto, cfg), inline=True)
        e.add_field(name="Efectivo", value=fmt_dinero(u["cash"], cfg), inline=True)
    return _mk_resp(embed=e)


def _eco_rob(guild, author, victima):
    if victima is None:
        return _mk_resp("❌ Debes indicar a quién robar. Uso: `.rob @usuario`")
    if victima.id == author.id:
        return _mk_resp("❌ No puedes robarte a ti mismo.")
    if victima.bot:
        return _mk_resp("❌ Los bots no llevan efectivo. Son pobres de verdad.")
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, author.id)
    carcel = econ_check_carcel(u, "rob")
    if carcel:
        return _mk_resp(carcel)
    ok, rest = econ_cooldown(u, "rob", ECONOMY_COOLDOWNS["rob"])
    if not ok:
        return _mk_resp(f"⏳ Aún te están buscando de tu último robo. Vuelve en {econ_fmt_segundos(rest)}.")
    v = get_user_econ(guild.id, victima.id)
    if v["cash"] <= 0:
        return _mk_resp(f"💸 {victima.display_name} no tiene efectivo. No hay nada que robar.")
    if random.random() < cfg["rob_fallo"]:
        carcel_seg = random.randint(30, 60) * 60
        u["jailed_until"] = time.time() + carcel_seg
        comp = min(u["cash"], random.randint(100, 300))
        u["cash"] -= comp
        v["cash"] += comp
        guardar_economy()
        e = discord.Embed(title="🚔 Te atraparon robando", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow())
        e.add_field(name="Condena", value=f"Cárcel {econ_fmt_segundos(carcel_seg)}", inline=True)
        e.add_field(name="Compensación a la víctima", value=fmt_dinero(comp, cfg), inline=True)
    else:
        frac = random.uniform(cfg["rob_min"], cfg["rob_max"])
        robado = max(1, int(v["cash"] * frac))
        v["cash"] -= robado
        u["cash"] += robado
        guardar_economy()
        e = discord.Embed(title="🏃 Robo exitoso", color=discord.Color.dark_green(), timestamp=discord.utils.utcnow())
        e.add_field(name="Robaste a", value=victima.mention, inline=True)
        e.add_field(name="Botín", value=fmt_dinero(robado, cfg), inline=True)
        e.add_field(name="Efectivo", value=fmt_dinero(u["cash"], cfg), inline=False)
    return _mk_resp(embed=e)


def _eco_transferir(guild, autor, destino, monto_str):
    if destino.id == autor.id:
        return _mk_resp("❌ No puedes pagarte a ti mismo.")
    if destino.bot:
        return _mk_resp("❌ No puedes pagar a un bot.")
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, autor.id)
    carcel = econ_check_carcel(u, "pay")
    if carcel:
        return _mk_resp(carcel)
    monto, err = parse_monto(monto_str, u["cash"])
    if err:
        return _mk_resp(f"❌ {err}")
    u["cash"] -= monto
    d = get_user_econ(guild.id, destino.id)
    d["cash"] += monto
    guardar_economy()
    e = discord.Embed(title="💸 Transferencia realizada", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    e.add_field(name="De", value=autor.mention, inline=True)
    e.add_field(name="Para", value=destino.mention, inline=True)
    e.add_field(name="Monto", value=fmt_dinero(monto, cfg), inline=True)
    return _mk_resp(embed=e)


def _eco_depositar(guild, autor, monto_str):
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, autor.id)
    carcel = econ_check_carcel(u, "deposit")
    if carcel:
        return _mk_resp(carcel)
    monto, err = parse_monto(monto_str, u["cash"])
    if err:
        return _mk_resp(f"❌ {err}")
    u["cash"] -= monto
    u["bank"] += monto
    guardar_economy()
    e = discord.Embed(title="🏦 Depósito realizado", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    e.add_field(name="Depositado", value=fmt_dinero(monto, cfg), inline=True)
    e.add_field(name="Banco", value=fmt_dinero(u["bank"], cfg), inline=True)
    return _mk_resp(embed=e)


def _eco_retirar(guild, autor, monto_str):
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, autor.id)
    carcel = econ_check_carcel(u, "withdraw")
    if carcel:
        return _mk_resp(carcel)
    monto, err = parse_monto(monto_str, u["bank"])
    if err:
        return _mk_resp(f"❌ {err}")
    u["bank"] -= monto
    u["cash"] += monto
    guardar_economy()
    e = discord.Embed(title="🏧 Retiro realizado", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    e.add_field(name="Retirado", value=fmt_dinero(monto, cfg), inline=True)
    e.add_field(name="Efectivo", value=fmt_dinero(u["cash"], cfg), inline=True)
    return _mk_resp(embed=e)


def _eco_shop_display(guild):
    cfg = get_econ_config(guild.id)
    items = shop_db.get(str(guild.id), {})
    if not items:
        return _mk_resp("🛒 La tienda está vacía. Un admin puede añadir items con `.shop add <item> <precio> [descripción]`.")
    lineas = []
    for nombre, info in items.items():
        desc = info.get("description", "")
        linea = f"• `{nombre}` — {fmt_dinero(info['price'], cfg)}"
        if desc:
            linea += f" — {desc}"
        lineas.append(linea)
    e = discord.Embed(title="🛒 Tienda del servidor", description="\n".join(lineas)[:4096], color=discord.Color.teal(), timestamp=discord.utils.utcnow())
    e.set_footer(text="Compra con .buy <item> [cantidad] • Vende con .sell (50% del precio)")
    return _mk_resp(embed=e)


def _eco_shop_add(guild, item, precio, desc=""):
    nombre = item.strip().lower()
    if not nombre or " " in nombre:
        return _mk_resp("❌ El nombre del item debe ser una sola palabra (sin espacios).")
    if precio is None or precio <= 0:
        return _mk_resp("❌ El precio debe ser un número mayor que 0.")
    shop_db.setdefault(str(guild.id), {})[nombre] = {"price": precio, "description": desc}
    guardar_shop()
    return _mk_resp(f"✅ Item `{nombre}` añadido a la tienda por {precio:,}.")


def _eco_shop_remove(guild, item):
    nombre = item.strip().lower()
    items = shop_db.get(str(guild.id), {})
    if nombre not in items:
        return _mk_resp(f"❌ El item `{nombre}` no está en la tienda.")
    del items[nombre]
    guardar_shop()
    return _mk_resp(f"✅ Item `{nombre}` eliminado de la tienda.")


def _eco_buy(guild, autor, item, cantidad):
    if cantidad is None or cantidad <= 0:
        return _mk_resp("❌ La cantidad debe ser mayor que 0.")
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, autor.id)
    carcel = econ_check_carcel(u, "buy")
    if carcel:
        return _mk_resp(carcel)
    nombre = item.strip().lower()
    items = shop_db.get(str(guild.id), {})
    if nombre not in items:
        return _mk_resp(f"❌ El item `{nombre}` no está en la tienda.")
    costo = items[nombre]["price"] * cantidad
    if u["cash"] < costo:
        return _mk_resp(f"❌ No tienes suficiente efectivo. Costo: {fmt_dinero(costo, cfg)}.")
    u["cash"] -= costo
    u["inventory"][nombre] = u["inventory"].get(nombre, 0) + cantidad
    guardar_economy()
    e = discord.Embed(title="🛍️ Compra realizada", color=discord.Color.teal(), timestamp=discord.utils.utcnow())
    e.add_field(name="Item", value=f"`{nombre}` x{cantidad}", inline=True)
    e.add_field(name="Costo", value=fmt_dinero(costo, cfg), inline=True)
    e.add_field(name="Efectivo", value=fmt_dinero(u["cash"], cfg), inline=False)
    return _mk_resp(embed=e)


def _eco_sell(guild, autor, item, cantidad):
    if cantidad is None or cantidad <= 0:
        return _mk_resp("❌ La cantidad debe ser mayor que 0.")
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, autor.id)
    carcel = econ_check_carcel(u, "sell")
    if carcel:
        return _mk_resp(carcel)
    nombre = item.strip().lower()
    tiene = u["inventory"].get(nombre, 0)
    if tiene < cantidad:
        return _mk_resp(f"❌ No tienes {cantidad} de `{nombre}` (tienes {tiene}).")
    items = shop_db.get(str(guild.id), {})
    precio_base = items.get(nombre, {}).get("price", 100)
    venta = (precio_base // 2) * cantidad
    u["inventory"][nombre] = tiene - cantidad
    if u["inventory"][nombre] <= 0:
        del u["inventory"][nombre]
    u["cash"] += venta
    guardar_economy()
    e = discord.Embed(title="📤 Venta realizada", color=discord.Color.teal(), timestamp=discord.utils.utcnow())
    e.add_field(name="Item", value=f"`{nombre}` x{cantidad}", inline=True)
    e.add_field(name="Recibiste", value=fmt_dinero(venta, cfg), inline=True)
    return _mk_resp(embed=e)


def _eco_inventory(guild, miembro):
    u = get_user_econ(guild.id, miembro.id)
    inv = u.get("inventory", {})
    if not inv:
        return _mk_resp(f"🎒 {miembro.display_name} no tiene items.")
    lineas = [f"• `{nombre}` x{cantidad}" for nombre, cantidad in inv.items()]
    e = discord.Embed(title=f"🎒 Inventario de {miembro.display_name}", description="\n".join(lineas)[:4096], color=discord.Color.dark_teal(), timestamp=discord.utils.utcnow())
    e.set_footer(text=f"Usa items con .use <item> • {len(inv)} tipo(s) de item")
    return _mk_resp(embed=e)


def _eco_use(guild, autor, item):
    if not item:
        return _mk_resp("❌ Debes indicar un item. Uso: `.use <item>`")
    nombre = item.strip().lower()
    u = get_user_econ(guild.id, autor.id)
    tiene = u["inventory"].get(nombre, 0)
    if tiene <= 0:
        return _mk_resp(f"❌ No tienes `{nombre}` en tu inventario.")
    u["inventory"][nombre] = tiene - 1
    if u["inventory"][nombre] <= 0:
        del u["inventory"][nombre]
    guardar_economy()
    mensajes = [
        f"Usaste `{nombre}`. Se sintió bien. 🌟",
        f"Usaste `{nombre}`. Nada espectacular pero ok. 🤷",
        f"Usaste `{nombre}` en una situación cuestionable. 😳",
        f"`{nombre}` usado con éxito. Nadie preguntó nada. 🤐",
    ]
    return _mk_resp(random.choice(mensajes))


def _eco_gift(guild, autor, destino, item, cantidad):
    if destino is None:
        return _mk_resp("❌ Debes indicar a quién regalar. Uso: `.gift @usuario <item> [cantidad]`")
    if destino.id == autor.id:
        return _mk_resp("❌ No puedes regalarte a ti mismo.")
    if destino.bot:
        return _mk_resp("❌ Los bots no aprecian los regalos.")
    if cantidad is None or cantidad <= 0:
        return _mk_resp("❌ La cantidad debe ser mayor que 0.")
    u = get_user_econ(guild.id, autor.id)
    carcel = econ_check_carcel(u, "gift")
    if carcel:
        return _mk_resp(carcel)
    nombre = item.strip().lower()
    tiene = u["inventory"].get(nombre, 0)
    if tiene < cantidad:
        return _mk_resp(f"❌ No tienes {cantidad} de `{nombre}` (tienes {tiene}).")
    u["inventory"][nombre] = tiene - cantidad
    if u["inventory"][nombre] <= 0:
        del u["inventory"][nombre]
    d = get_user_econ(guild.id, destino.id)
    d["inventory"][nombre] = d["inventory"].get(nombre, 0) + cantidad
    guardar_economy()
    e = discord.Embed(title="🎁 Regalo entregado", color=discord.Color.magenta(), timestamp=discord.utils.utcnow())
    e.add_field(name="De", value=autor.mention, inline=True)
    e.add_field(name="Para", value=destino.mention, inline=True)
    e.add_field(name="Item", value=f"`{nombre}` x{cantidad}", inline=True)
    return _mk_resp(embed=e)


# ---------- 🎰 Juegos ----------

SLOTS_EMOJIS = ["🍒", "🍋", "🍇", "🔔", "⭐", "💎"]
SLOTS_MULT = {"💎": 10, "⭐": 6, "🔔": 4, "🍇": 3, "🍋": 2, "🍒": 2}
RULETA_ROJOS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34}


def _eco_slots(guild, autor, monto_str):
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, autor.id)
    carcel = econ_check_carcel(u, "slots")
    if carcel:
        return _mk_resp(carcel)
    monto, err = parse_monto(monto_str, u["cash"])
    if err:
        return _mk_resp(f"❌ {err}")
    u["cash"] -= monto
    r = [random.choice(SLOTS_EMOJIS) for _ in range(3)]
    if r[0] == r[1] == r[2]:
        premio = monto * SLOTS_MULT[r[0]]
        resultado = f"🎉 ¡TRES {r[0]} IGUALES! Premio x{SLOTS_MULT[r[0]]}"
        color = discord.Color.green()
    elif r[0] == r[1] or r[1] == r[2] or r[0] == r[2]:
        premio = monto + monto // 2
        resultado = "✨ ¡Dos iguales! Premio x1.5"
        color = discord.Color.gold()
    else:
        premio = 0
        resultado = "💀 Sin suerte..."
        color = discord.Color.dark_red()
    u["cash"] += premio
    guardar_economy()
    e = discord.Embed(title="🎰 Tragaperras", description=f"┃ {r[0]} ┃ {r[1]} ┃ {r[2]} ┃", color=color, timestamp=discord.utils.utcnow())
    e.add_field(name="Apuesta", value=fmt_dinero(monto, cfg), inline=True)
    e.add_field(name="Premio", value=fmt_dinero(premio, cfg), inline=True)
    e.add_field(name="Efectivo", value=fmt_dinero(u["cash"], cfg), inline=False)
    e.add_field(name="Resultado", value=resultado, inline=False)
    return _mk_resp(embed=e)


def _eco_coinflip(guild, autor, eleccion, monto_str):
    if eleccion not in ("cara", "cruz"):
        return _mk_resp("❌ Debes elegir `cara` o `cruz`. Uso: `.coinflip <cara|cruz> <monto|all>`")
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, autor.id)
    carcel = econ_check_carcel(u, "coinflip")
    if carcel:
        return _mk_resp(carcel)
    monto, err = parse_monto(monto_str, u["cash"])
    if err:
        return _mk_resp(f"❌ {err}")
    u["cash"] -= monto
    resultado_txt = random.choice(["cara", "cruz"])
    if resultado_txt == eleccion:
        premio = int(monto * 1.95)
        texto = f"🎉 ¡{resultado_txt.capitalize()}! Ganas {fmt_dinero(premio, cfg)}."
        color = discord.Color.green()
    else:
        premio = 0
        texto = f"💀 Salió {resultado_txt}. Pierdes tu apuesta."
        color = discord.Color.dark_red()
    u["cash"] += premio
    guardar_economy()
    e = discord.Embed(title="🪙 Lanzamiento de moneda", description=f"Resultó: **{resultado_txt.capitalize()}**", color=color, timestamp=discord.utils.utcnow())
    e.add_field(name="Elegiste", value=eleccion.capitalize(), inline=True)
    e.add_field(name="Apuesta", value=fmt_dinero(monto, cfg), inline=True)
    e.add_field(name="Resultado", value=texto, inline=False)
    return _mk_resp(embed=e)


def _eco_dice(guild, autor, numero, monto_str):
    if numero is None or not (1 <= numero <= 6):
        return _mk_resp("❌ Debes elegir un número del 1 al 6. Uso: `.dice <1-6> <monto|all>`")
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, autor.id)
    carcel = econ_check_carcel(u, "dice")
    if carcel:
        return _mk_resp(carcel)
    monto, err = parse_monto(monto_str, u["cash"])
    if err:
        return _mk_resp(f"❌ {err}")
    u["cash"] -= monto
    tirada = random.randint(1, 6)
    if tirada == numero:
        premio = monto * 5
        texto = f"🎉 ¡{tirada}! Acertaste. Ganas {fmt_dinero(premio, cfg)} (x5)."
        color = discord.Color.green()
    else:
        premio = 0
        texto = f"💀 Salió {tirada}. Fallaste."
        color = discord.Color.dark_red()
    u["cash"] += premio
    guardar_economy()
    e = discord.Embed(title="🎲 Dado", description=f"Tirada: **{tirada}**", color=color, timestamp=discord.utils.utcnow())
    e.add_field(name="Elegiste", value=str(numero), inline=True)
    e.add_field(name="Apuesta", value=fmt_dinero(monto, cfg), inline=True)
    e.add_field(name="Resultado", value=texto, inline=False)
    return _mk_resp(embed=e)


def _eco_roulette(guild, autor, apuesta, monto_str):
    if apuesta not in ("rojo", "negro", "verde"):
        return _mk_resp("❌ Debes apostar a `rojo`, `negro` o `verde`. Uso: `.roulette <rojo|negro|verde> <monto|all>`")
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, autor.id)
    carcel = econ_check_carcel(u, "roulette")
    if carcel:
        return _mk_resp(carcel)
    monto, err = parse_monto(monto_str, u["cash"])
    if err:
        return _mk_resp(f"❌ {err}")
    u["cash"] -= monto
    n = random.randint(0, 36)
    if n == 0:
        color_salido = "verde"
    elif n in RULETA_ROJOS:
        color_salido = "rojo"
    else:
        color_salido = "negro"
    if color_salido == apuesta:
        mult = 14 if apuesta == "verde" else 2
        premio = monto * mult
        texto = f"🎉 ¡{color_salido.capitalize()} ({n})! Ganas {fmt_dinero(premio, cfg)} (x{mult})."
        color = discord.Color.green() if apuesta != "verde" else discord.Color.dark_green()
    else:
        premio = 0
        texto = f"💀 Salió {color_salido} ({n}). Pierdes tu apuesta."
        color = discord.Color.dark_red()
    u["cash"] += premio
    guardar_economy()
    emoji = "🔴" if color_salido == "rojo" else ("⚫" if color_salido == "negro" else "🟢")
    e = discord.Embed(title="🎡 Ruleta", description=f"{emoji} Salió: **{color_salido.capitalize()} ({n})**", color=color, timestamp=discord.utils.utcnow())
    e.add_field(name="Apuestaste a", value=apuesta.capitalize(), inline=True)
    e.add_field(name="Monto", value=fmt_dinero(monto, cfg), inline=True)
    e.add_field(name="Resultado", value=texto, inline=False)
    return _mk_resp(embed=e)


# ---------- 🃏 Blackjack y Mayor/Menor (interactivos con botones) ----------

def _bj_carta():
    return random.choice(["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"])


def _bj_valor(mano):
    total = 0
    ases = 0
    for c in mano:
        if c == "A":
            total += 11
            ases += 1
        elif c in ("J", "Q", "K"):
            total += 10
        else:
            total += int(c)
    while total > 21 and ases > 0:
        total -= 10
        ases -= 1
    return total


def _bj_mano_str(mano):
    return " ".join(mano)


def _hl_carta_str(n):
    return {1: "A", 11: "J", 12: "Q", 13: "K"}.get(n, str(n))


class BlackjackView(discord.ui.View):
    def __init__(self, guild, user, monto):
        super().__init__(timeout=120)
        self.guild = guild
        self.user = user
        self.monto = monto
        self.cfg = get_econ_config(guild.id)
        self.mano = [_bj_carta(), _bj_carta()]
        self.crupier = [_bj_carta(), _bj_carta()]
        self.msg = None
        self.terminado = False

    def _embed(self, ocultar=True, resultado=None):
        pc = _bj_valor(self.mano)
        cc = _bj_valor(self.crupier)
        if ocultar:
            crup = f"{self.crupier[0]} 🂠 (?)"
        else:
            crup = f"{_bj_mano_str(self.crupier)} ({cc})"
        e = discord.Embed(title="🃏 Blackjack", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        e.add_field(name=f"Tu mano ({pc})", value=_bj_mano_str(self.mano), inline=False)
        e.add_field(name="Crupier", value=crup, inline=False)
        e.add_field(name="Apuesta", value=fmt_dinero(self.monto, self.cfg), inline=True)
        if resultado:
            e.add_field(name="Resultado", value=resultado, inline=False)
        return e

    def _settle(self):
        u = get_user_econ(self.guild.id, self.user.id)
        pc = _bj_valor(self.mano)
        cc = _bj_valor(self.crupier)
        if pc > 21:
            return f"💥 Te pasaste de 21. Pierdes {fmt_dinero(self.monto, self.cfg)}.", discord.Color.dark_red()
        if cc > 21 or pc > cc:
            premio = self.monto * 2
            u["cash"] += premio
            guardar_economy()
            return f"🎉 ¡Ganas {fmt_dinero(premio, self.cfg)}!", discord.Color.green()
        if pc == cc:
            u["cash"] += self.monto
            guardar_economy()
            return "🤝 Empate. Recuperas tu apuesta.", discord.Color.gold()
        return f"💀 El crupier gana. Pierdes {fmt_dinero(self.monto, self.cfg)}.", discord.Color.dark_red()

    async def _finalizar(self, interaction, texto, color):
        self.terminado = True
        self.stop()
        embed = self._embed(ocultar=False, resultado=texto)
        embed.color = color
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=None)
        elif self.msg is not None:
            try:
                await self.msg.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="📥 Pedir", style=discord.ButtonStyle.green)
    async def pedir(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("❌ No es tu partida.", ephemeral=True)
        self.mano.append(_bj_carta())
        if _bj_valor(self.mano) > 21:
            texto, color = self._settle()
            await self._finalizar(interaction, texto, color)
        else:
            await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="✋ Plantarse", style=discord.ButtonStyle.red)
    async def plantarse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("❌ No es tu partida.", ephemeral=True)
        while _bj_valor(self.crupier) < 17:
            self.crupier.append(_bj_carta())
        texto, color = self._settle()
        await self._finalizar(interaction, texto, color)

    async def on_timeout(self):
        if self.terminado:
            return
        while _bj_valor(self.crupier) < 17:
            self.crupier.append(_bj_carta())
        texto, color = self._settle()
        await self._finalizar(None, texto, color)


class HighlowView(discord.ui.View):
    def __init__(self, guild, user, monto):
        super().__init__(timeout=60)
        self.guild = guild
        self.user = user
        self.monto = monto
        self.cfg = get_econ_config(guild.id)
        self.carta = random.randint(1, 13)
        self.msg = None
        self.terminado = False

    def _embed(self, resultado=None, nueva=None, color=discord.Color.gold()):
        e = discord.Embed(title="🃏 Mayor o Menor", color=color, timestamp=discord.utils.utcnow())
        e.add_field(name="Carta actual", value=f"**{_hl_carta_str(self.carta)}**", inline=True)
        if nueva is not None:
            e.add_field(name="Nueva carta", value=f"**{_hl_carta_str(nueva)}**", inline=True)
        e.add_field(name="Apuesta", value=fmt_dinero(self.monto, self.cfg), inline=True)
        if resultado:
            e.add_field(name="Resultado", value=resultado, inline=False)
        else:
            e.add_field(name="¿La siguiente será mayor o menor?", value="Paga x1.9 • Igual = empate", inline=False)
        return e

    def _resolver(self, eleccion):
        self.terminado = True
        self.stop()
        nueva = random.randint(1, 13)
        u = get_user_econ(self.guild.id, self.user.id)
        if nueva == self.carta:
            u["cash"] += self.monto
            texto = "🤝 Igual. Recuperas tu apuesta."
            color = discord.Color.gold()
        elif (nueva > self.carta) == (eleccion == "mayor"):
            premio = int(self.monto * 1.9)
            u["cash"] += premio
            texto = f"🎉 ¡Correcto! Ganas {fmt_dinero(premio, self.cfg)}."
            color = discord.Color.green()
        else:
            texto = f"💀 Fallaste. Pierdes {fmt_dinero(self.monto, self.cfg)}."
            color = discord.Color.dark_red()
        guardar_economy()
        return nueva, texto, color

    async def _boton(self, interaction, eleccion):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("❌ No es tu partida.", ephemeral=True)
        nueva, texto, color = self._resolver(eleccion)
        await interaction.response.edit_message(embed=self._embed(resultado=texto, nueva=nueva, color=color), view=None)

    @discord.ui.button(label="⬆️ Mayor", style=discord.ButtonStyle.green)
    async def mayor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._boton(interaction, "mayor")

    @discord.ui.button(label="⬇️ Menor", style=discord.ButtonStyle.red)
    async def menor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._boton(interaction, "menor")

    async def on_timeout(self):
        if self.terminado:
            return
        self.terminado = True
        self.stop()
        u = get_user_econ(self.guild.id, self.user.id)
        u["cash"] += self.monto
        guardar_economy()
        if self.msg is not None:
            try:
                await self.msg.edit(embed=self._embed(resultado="⌛ Tiempo agotado. Se te devolvió la apuesta.", color=discord.Color.dark_grey()), view=None)
            except discord.HTTPException:
                pass


def _eco_blackjack_start(guild, user, monto_str):
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, user.id)
    carcel = econ_check_carcel(u, "blackjack")
    if carcel:
        return ("msg", _mk_resp(carcel))
    monto, err = parse_monto(monto_str, u["cash"])
    if err:
        return ("msg", _mk_resp(f"❌ {err}"))
    u["cash"] -= monto
    view = BlackjackView(guild, user, monto)
    if _bj_valor(view.mano) == 21:
        premio = int(monto * 2.5)
        u["cash"] += premio
        guardar_economy()
        e = discord.Embed(title="🃏 Blackjack", description="¡BLACKJACK NATURAL! 🎉", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        e.add_field(name="Tu mano (21)", value=_bj_mano_str(view.mano), inline=False)
        e.add_field(name="Premio", value=fmt_dinero(premio, cfg), inline=True)
        return ("msg", {"embed": e})
    guardar_economy()
    return ("view", (view._embed(), view))


def _eco_highlow_start(guild, user, monto_str):
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, user.id)
    carcel = econ_check_carcel(u, "highlow")
    if carcel:
        return ("msg", _mk_resp(carcel))
    monto, err = parse_monto(monto_str, u["cash"])
    if err:
        return ("msg", _mk_resp(f"❌ {err}"))
    u["cash"] -= monto
    guardar_economy()
    view = HighlowView(guild, user, monto)
    return ("view", (view._embed(), view))


# ---------- 🏆 Ranking y admin ----------

def _eco_baltop(guild):
    cfg = get_econ_config(guild.id)
    gdata = economy_db.get(str(guild.id), {})
    usuarios = [(int(uid), u["cash"] + u["bank"]) for uid, u in gdata.items()]
    usuarios.sort(key=lambda x: -x[1])
    if not usuarios:
        return _mk_resp("📭 Todavía nadie tiene dinero en este servidor.")
    medals = ["🥇", "🥈", "🥉"]
    lineas = []
    for i, (uid, total) in enumerate(usuarios[:10]):
        prefijo = medals[i] if i < 3 else f"**{i + 1}.**"
        lineas.append(f"{prefijo} <@{uid}> — {fmt_dinero(total, cfg)}")
    e = discord.Embed(title="🏆 Top de ricos", description="\n".join(lineas), color=discord.Color.gold(), timestamp=discord.utils.utcnow())
    e.set_footer(text="Se ordena por dinero total (efectivo + banco)")
    return _mk_resp(embed=e)


def _eco_admin_money(guild, accion, miembro, monto_str):
    monto = _parse_entero(monto_str)
    if monto is None:
        return _mk_resp("❌ Monto inválido.")
    cfg = get_econ_config(guild.id)
    u = get_user_econ(guild.id, miembro.id)
    if accion == "add":
        u["cash"] += monto
    elif accion == "remove":
        if u["cash"] < monto:
            return _mk_resp(f"❌ {miembro.display_name} solo tiene {fmt_dinero(u['cash'], cfg)} en efectivo.")
        u["cash"] -= monto
    else:
        u["cash"] = monto
    guardar_economy()
    verbos = {"add": "Añadido", "remove": "Quitado", "set": "Fijado"}
    e = discord.Embed(title="💰 Balance modificado", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
    e.add_field(name="Usuario", value=f"{miembro} (`{miembro.id}`)", inline=False)
    e.add_field(name=verbos[accion], value=fmt_dinero(monto, cfg), inline=True)
    e.add_field(name="Nuevo efectivo", value=fmt_dinero(u["cash"], cfg), inline=True)
    return _mk_resp(embed=e)


def _eco_set_currency(guild, simbolo):
    s = (simbolo or "").strip()
    if not s or len(s) > 10:
        return _mk_resp("❌ El símbolo debe tener entre 1 y 10 caracteres.")
    cfg = get_econ_config(guild.id)
    cfg["currency"] = s
    guardar_economy()
    return _mk_resp(f"✅ Moneda cambiada a `{s}`.")


def _eco_set_start_balance(guild, monto_str):
    monto = _parse_entero(monto_str)
    if monto is None or monto < 0:
        return _mk_resp("❌ El balance inicial debe ser un número mayor o igual a 0.")
    cfg = get_econ_config(guild.id)
    cfg["start_balance"] = monto
    guardar_economy()
    return _mk_resp(f"✅ Balance inicial fijado en {monto:,} (solo aplica a usuarios nuevos).")


def _eco_reset(guild):
    gid = str(guild.id)
    if gid in economy_db:
        del economy_db[gid]
    guardar_economy()
    return _mk_resp("✅ Economía del servidor reseteada. Todos empiezan de cero.")


def _eco_config_view(guild):
    cfg = get_econ_config(guild.id)
    e = discord.Embed(title="⚙️ Configuración de economía", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    for k, v in cfg.items():
        e.add_field(name=k, value=str(v), inline=True)
    e.set_footer(text="Cambiable con .set-currency, .set-start-balance, etc.")
    return _mk_resp(embed=e)


# ============================================================
# 💰 COMANDOS DE ECONOMÍA — prefijo (.)
# ============================================================

@bot.command(name="balance", aliases=["bal", "money", "dinero"])
@commands.guild_only()
async def eco_balance(ctx, miembro: discord.Member = None):
    """Muestra el balance (efectivo, banco y total). Uso: .balance [@usuario]"""
    await ctx.send(**_eco_balance(ctx.guild, miembro or ctx.author))


@bot.command(name="pay", aliases=["give", "pagar", "dar"])
@commands.guild_only()
async def eco_pay(ctx, miembro: discord.Member = None, *, monto: str = ""):
    """Transfiere dinero a otro usuario. Uso: .pay @usuario <monto|all|mitad>"""
    if miembro is None:
        return await ctx.send("❌ Debes indicar un usuario. Uso: `.pay @usuario <monto|all>`")
    await ctx.send(**_eco_transferir(ctx.guild, ctx.author, miembro, monto))


@bot.command(name="daily", aliases=["diario"])
@commands.guild_only()
async def eco_daily(ctx):
    """Claim tu recompensa diaria. Uso: .daily"""
    await ctx.send(**_eco_periodico(ctx.guild, ctx.author, "daily"))


@bot.command(name="weekly", aliases=["semanal"])
@commands.guild_only()
async def eco_weekly(ctx):
    """Claim tu recompensa semanal. Uso: .weekly"""
    await ctx.send(**_eco_periodico(ctx.guild, ctx.author, "weekly"))


@bot.command(name="monthly", aliases=["mensual"])
@commands.guild_only()
async def eco_monthly(ctx):
    """Claim tu recompensa mensual. Uso: .monthly"""
    await ctx.send(**_eco_periodico(ctx.guild, ctx.author, "monthly"))


@bot.command(name="work", aliases=["trabajar"])
@commands.guild_only()
async def eco_work(ctx):
    """Trabaja para ganar dinero (cooldown 1h). Uso: .work"""
    await ctx.send(**_eco_work(ctx.guild, ctx.author))


@bot.command(name="crime", aliases=["crimen", "delito"])
@commands.guild_only()
async def eco_crime(ctx):
    """Comete un crimen: gana dinero o acaba en la cárcel. Uso: .crime"""
    await ctx.send(**_eco_crime(ctx.guild, ctx.author))


@bot.command(name="slut")
@commands.guild_only()
async def eco_slut(ctx):
    """Gana dinero con trabajos extraños (arriesgado). Uso: .slut"""
    await ctx.send(**_eco_slut(ctx.guild, ctx.author))


@bot.command(name="rob", aliases=["robar", "steal"])
@commands.guild_only()
async def eco_rob(ctx, miembro: discord.Member = None):
    """Roba efectivo a otro usuario. Uso: .rob @usuario"""
    if miembro is None:
        return await ctx.send("❌ Debes indicar a quién robar. Uso: `.rob @usuario`")
    await ctx.send(**_eco_rob(ctx.guild, ctx.author, miembro))


@bot.command(name="deposit", aliases=["dep", "ingresar"])
@commands.guild_only()
async def eco_deposit(ctx, *, monto: str = ""):
    """Deposita efectivo en el banco. Uso: .deposit <monto|all|mitad>"""
    await ctx.send(**_eco_depositar(ctx.guild, ctx.author, monto))


@bot.command(name="withdraw", aliases=["with", "retirar"])
@commands.guild_only()
async def eco_withdraw(ctx, *, monto: str = ""):
    """Retira dinero del banco. Uso: .withdraw <monto|all|mitad>"""
    await ctx.send(**_eco_retirar(ctx.guild, ctx.author, monto))


@bot.command(name="shop", aliases=["tienda"])
@commands.guild_only()
async def eco_shop(ctx, *, args: str = ""):
    """Muestra la tienda. Admins: .shop add <item> <precio> [desc] | .shop remove <item>"""
    tokens = args.split()
    if tokens and tokens[0].lower() in ("add", "añadir"):
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.send("❌ Necesitas el permiso Manage Server.")
        if len(tokens) < 3:
            return await ctx.send("❌ Uso: `.shop add <item> <precio> [descripción]`")
        precio = _parse_entero(tokens[2])
        desc = " ".join(tokens[3:]).strip()
        await ctx.send(**_eco_shop_add(ctx.guild, tokens[1], precio, desc))
    elif tokens and tokens[0].lower() in ("remove", "eliminar", "rm"):
        if not ctx.author.guild_permissions.manage_guild:
            return await ctx.send("❌ Necesitas el permiso Manage Server.")
        if len(tokens) < 2:
            return await ctx.send("❌ Uso: `.shop remove <item>`")
        await ctx.send(**_eco_shop_remove(ctx.guild, tokens[1]))
    else:
        await ctx.send(**_eco_shop_display(ctx.guild))


@bot.command(name="buy", aliases=["comprar"])
@commands.guild_only()
async def eco_buy(ctx, item: str = "", cantidad: str = "1"):
    """Compra un item de la tienda. Uso: .buy <item> [cantidad]"""
    if not item:
        return await ctx.send("❌ Debes indicar un item. Uso: `.buy <item> [cantidad]`")
    await ctx.send(**_eco_buy(ctx.guild, ctx.author, item, _parse_entero(cantidad) or 1))


@bot.command(name="sell", aliases=["vender"])
@commands.guild_only()
async def eco_sell(ctx, item: str = "", cantidad: str = "1"):
    """Vende un item de tu inventario (50% del precio). Uso: .sell <item> [cantidad]"""
    if not item:
        return await ctx.send("❌ Debes indicar un item. Uso: `.sell <item> [cantidad]`")
    await ctx.send(**_eco_sell(ctx.guild, ctx.author, item, _parse_entero(cantidad) or 1))


@bot.command(name="inventory", aliases=["inv", "inventario"])
@commands.guild_only()
async def eco_inventory(ctx, miembro: discord.Member = None):
    """Muestra tu inventario. Uso: .inventory [@usuario]"""
    await ctx.send(**_eco_inventory(ctx.guild, miembro or ctx.author))


@bot.command(name="use", aliases=["usar"])
@commands.guild_only()
async def eco_use(ctx, *, item: str = ""):
    """Usa un item de tu inventario. Uso: .use <item>"""
    await ctx.send(**_eco_use(ctx.guild, ctx.author, item))


@bot.command(name="gift", aliases=["regalar"])
@commands.guild_only()
async def eco_gift(ctx, miembro: discord.Member = None, item: str = "", cantidad: str = "1"):
    """Regala un item a otro usuario. Uso: .gift @usuario <item> [cantidad]"""
    if miembro is None or not item:
        return await ctx.send("❌ Uso: `.gift @usuario <item> [cantidad]`")
    await ctx.send(**_eco_gift(ctx.guild, ctx.author, miembro, item, _parse_entero(cantidad) or 1))


@bot.command(name="slots")
@commands.guild_only()
async def eco_slots(ctx, *, monto: str = ""):
    """Juega a las tragaperras. Uso: .slots <monto|all>"""
    await ctx.send(**_eco_slots(ctx.guild, ctx.author, monto))


@bot.command(name="coinflip", aliases=["cf", "moneda"])
@commands.guild_only()
async def eco_coinflip(ctx, eleccion: str = "", *, monto: str = ""):
    """Apuesta al lanzamiento de una moneda (x1.95). Uso: .coinflip <cara|cruz> <monto|all>"""
    await ctx.send(**_eco_coinflip(ctx.guild, ctx.author, (eleccion or "").lower(), monto))


@bot.command(name="dice", aliases=["dado"])
@commands.guild_only()
async def eco_dice(ctx, numero: str = "", *, monto: str = ""):
    """Apuesta a un número del dado (paga x5). Uso: .dice <1-6> <monto|all>"""
    await ctx.send(**_eco_dice(ctx.guild, ctx.author, _parse_entero(numero), monto))


@bot.command(name="highlow", aliases=["hl", "altobajo"])
@commands.guild_only()
async def eco_highlow(ctx, *, monto: str = ""):
    """Adivina si la carta es mayor o menor (x1.9). Uso: .highlow <monto|all>"""
    res = _eco_highlow_start(ctx.guild, ctx.author, monto)
    if res[0] == "msg":
        return await ctx.send(**res[1])
    embed, view = res[1]
    view.msg = await ctx.send(embed=embed, view=view)


@bot.command(name="roulette", aliases=["ruleta"])
@commands.guild_only()
async def eco_roulette(ctx, apuesta: str = "", *, monto: str = ""):
    """Apuesta en la ruleta (rojo/negro x2, verde x14). Uso: .roulette <rojo|negro|verde> <monto|all>"""
    await ctx.send(**_eco_roulette(ctx.guild, ctx.author, (apuesta or "").lower(), monto))


@bot.command(name="blackjack", aliases=["bj", "21"])
@commands.guild_only()
async def eco_blackjack(ctx, *, monto: str = ""):
    """Juega al Blackjack contra el crupier (x2, natural x2.5). Uso: .blackjack <monto|all>"""
    res = _eco_blackjack_start(ctx.guild, ctx.author, monto)
    if res[0] == "msg":
        return await ctx.send(**res[1])
    embed, view = res[1]
    view.msg = await ctx.send(embed=embed, view=view)


@bot.command(name="baltop", aliases=["moneyleaderboard", "ricos"])
@commands.guild_only()
async def eco_baltop(ctx):
    """Muestra el top de usuarios más ricos. Uso: .baltop"""
    await ctx.send(**_eco_baltop(ctx.guild))


@bot.command(name="add-money")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def eco_add_money(ctx, miembro: discord.Member = None, *, monto: str = ""):
    """Añade dinero a un usuario. Uso: .add-money @usuario <monto>"""
    if miembro is None:
        return await ctx.send("❌ Uso: `.add-money @usuario <monto>`")
    await ctx.send(**_eco_admin_money(ctx.guild, "add", miembro, monto))


@bot.command(name="remove-money")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def eco_remove_money(ctx, miembro: discord.Member = None, *, monto: str = ""):
    """Quita dinero a un usuario. Uso: .remove-money @usuario <monto>"""
    if miembro is None:
        return await ctx.send("❌ Uso: `.remove-money @usuario <monto>`")
    await ctx.send(**_eco_admin_money(ctx.guild, "remove", miembro, monto))


@bot.command(name="set-money")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def eco_set_money(ctx, miembro: discord.Member = None, *, monto: str = ""):
    """Fija el efectivo de un usuario. Uso: .set-money @usuario <monto>"""
    if miembro is None:
        return await ctx.send("❌ Uso: `.set-money @usuario <monto>`")
    await ctx.send(**_eco_admin_money(ctx.guild, "set", miembro, monto))


@bot.command(name="set-currency")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def eco_set_currency(ctx, *, simbolo: str = ""):
    """Cambia el símbolo de la moneda. Uso: .set-currency <símbolo>"""
    await ctx.send(**_eco_set_currency(ctx.guild, simbolo))


@bot.command(name="set-start-balance")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def eco_set_start_balance(ctx, *, monto: str = ""):
    """Fija el balance inicial para usuarios nuevos. Uso: .set-start-balance <monto>"""
    await ctx.send(**_eco_set_start_balance(ctx.guild, monto))


@bot.command(name="economy-config", aliases=["econ-config"])
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def eco_config(ctx):
    """Muestra la configuración de economía. Uso: .economy-config"""
    await ctx.send(**_eco_config_view(ctx.guild))


@bot.command(name="reset-economy")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def eco_reset(ctx, *, confirmar: str = ""):
    """Resetea TODA la economía del servidor. Uso: .reset-economy confirmar"""
    if confirmar.lower() not in ("confirmar", "confirm", "si", "sí"):
        return await ctx.send("⚠️ Esto BORRARÁ todo el dinero de todos los usuarios.\nEscribe `.reset-economy confirmar` para confirmar.")
    await ctx.send(**_eco_reset(ctx.guild))


# ============================================================
# 💰 COMANDOS DE ECONOMÍA — slash (/)
# ============================================================

@bot.tree.command(name="balance", description="Muestra el balance (efectivo, banco y total)")
@app_commands.guild_only()
@app_commands.describe(miembro="Usuario cuyo balance quieres ver (opcional)")
async def slash_balance(interaction: discord.Interaction, miembro: discord.Member = None):
    await interaction.response.send_message(**_eco_balance(interaction.guild, miembro or interaction.user))


@bot.tree.command(name="pay", description="Transfiere dinero a otro usuario")
@app_commands.guild_only()
@app_commands.describe(miembro="Destinatario", monto="Monto (número, 'all' o 'mitad')")
async def slash_pay(interaction: discord.Interaction, miembro: discord.Member, monto: str):
    await interaction.response.send_message(**_eco_transferir(interaction.guild, interaction.user, miembro, monto))


@bot.tree.command(name="daily", description="Claim tu recompensa diaria")
@app_commands.guild_only()
async def slash_daily(interaction: discord.Interaction):
    await interaction.response.send_message(**_eco_periodico(interaction.guild, interaction.user, "daily"))


@bot.tree.command(name="weekly", description="Claim tu recompensa semanal")
@app_commands.guild_only()
async def slash_weekly(interaction: discord.Interaction):
    await interaction.response.send_message(**_eco_periodico(interaction.guild, interaction.user, "weekly"))


@bot.tree.command(name="monthly", description="Claim tu recompensa mensual")
@app_commands.guild_only()
async def slash_monthly(interaction: discord.Interaction):
    await interaction.response.send_message(**_eco_periodico(interaction.guild, interaction.user, "monthly"))


@bot.tree.command(name="work", description="Trabaja para ganar dinero (cooldown 1h)")
@app_commands.guild_only()
async def slash_work(interaction: discord.Interaction):
    await interaction.response.send_message(**_eco_work(interaction.guild, interaction.user))


@bot.tree.command(name="crime", description="Comete un crimen: gana dinero o acaba en la cárcel")
@app_commands.guild_only()
async def slash_crime(interaction: discord.Interaction):
    await interaction.response.send_message(**_eco_crime(interaction.guild, interaction.user))


@bot.tree.command(name="slut", description="Gana dinero con trabajos extraños (arriesgado)")
@app_commands.guild_only()
async def slash_slut(interaction: discord.Interaction):
    await interaction.response.send_message(**_eco_slut(interaction.guild, interaction.user))


@bot.tree.command(name="rob", description="Roba efectivo a otro usuario")
@app_commands.guild_only()
@app_commands.describe(miembro="A quién robar")
async def slash_rob(interaction: discord.Interaction, miembro: discord.Member):
    await interaction.response.send_message(**_eco_rob(interaction.guild, interaction.user, miembro))


@bot.tree.command(name="deposit", description="Deposita efectivo en el banco")
@app_commands.guild_only()
@app_commands.describe(monto="Monto (número, 'all' o 'mitad')")
async def slash_deposit(interaction: discord.Interaction, monto: str):
    await interaction.response.send_message(**_eco_depositar(interaction.guild, interaction.user, monto))


@bot.tree.command(name="withdraw", description="Retira dinero del banco")
@app_commands.guild_only()
@app_commands.describe(monto="Monto (número, 'all' o 'mitad')")
async def slash_withdraw(interaction: discord.Interaction, monto: str):
    await interaction.response.send_message(**_eco_retirar(interaction.guild, interaction.user, monto))


@bot.tree.command(name="shop", description="Muestra la tienda del servidor")
@app_commands.guild_only()
async def slash_shop(interaction: discord.Interaction):
    await interaction.response.send_message(**_eco_shop_display(interaction.guild))


@bot.tree.command(name="shop-add", description="Añade un item a la tienda (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(item="Nombre del item (una palabra)", precio="Precio del item", descripcion="Descripción (opcional)")
async def slash_shop_add(interaction: discord.Interaction, item: str, precio: int, descripcion: str = ""):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    await interaction.response.send_message(**_eco_shop_add(interaction.guild, item, precio, descripcion))


@bot.tree.command(name="shop-remove", description="Quita un item de la tienda (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(item="Nombre del item a quitar")
async def slash_shop_remove(interaction: discord.Interaction, item: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    await interaction.response.send_message(**_eco_shop_remove(interaction.guild, item))


@bot.tree.command(name="buy", description="Compra un item de la tienda")
@app_commands.guild_only()
@app_commands.describe(item="Nombre del item", cantidad="Cantidad (por defecto 1)")
async def slash_buy(interaction: discord.Interaction, item: str, cantidad: app_commands.Range[int, 1] = 1):
    await interaction.response.send_message(**_eco_buy(interaction.guild, interaction.user, item, cantidad))


@bot.tree.command(name="sell", description="Vende un item de tu inventario (50% del precio)")
@app_commands.guild_only()
@app_commands.describe(item="Nombre del item", cantidad="Cantidad (por defecto 1)")
async def slash_sell(interaction: discord.Interaction, item: str, cantidad: app_commands.Range[int, 1] = 1):
    await interaction.response.send_message(**_eco_sell(interaction.guild, interaction.user, item, cantidad))


@bot.tree.command(name="inventory", description="Muestra tu inventario")
@app_commands.guild_only()
@app_commands.describe(miembro="Usuario cuyo inventario ver (opcional)")
async def slash_inventory(interaction: discord.Interaction, miembro: discord.Member = None):
    await interaction.response.send_message(**_eco_inventory(interaction.guild, miembro or interaction.user))


@bot.tree.command(name="use", description="Usa un item de tu inventario")
@app_commands.guild_only()
@app_commands.describe(item="Nombre del item")
async def slash_use(interaction: discord.Interaction, item: str):
    await interaction.response.send_message(**_eco_use(interaction.guild, interaction.user, item))


@bot.tree.command(name="gift", description="Regala un item a otro usuario")
@app_commands.guild_only()
@app_commands.describe(miembro="Destinatario", item="Nombre del item", cantidad="Cantidad (por defecto 1)")
async def slash_gift(interaction: discord.Interaction, miembro: discord.Member, item: str, cantidad: app_commands.Range[int, 1] = 1):
    await interaction.response.send_message(**_eco_gift(interaction.guild, interaction.user, miembro, item, cantidad))


@bot.tree.command(name="slots", description="Juega a las tragaperras")
@app_commands.guild_only()
@app_commands.describe(monto="Cuánto apostar (número, 'all' o 'mitad')")
async def slash_slots(interaction: discord.Interaction, monto: str):
    await interaction.response.send_message(**_eco_slots(interaction.guild, interaction.user, monto))


@bot.tree.command(name="coinflip", description="Apuesta al lanzamiento de una moneda (x1.95)")
@app_commands.guild_only()
@app_commands.choices(eleccion=[
    app_commands.Choice(name="Cara", value="cara"),
    app_commands.Choice(name="Cruz", value="cruz"),
])
@app_commands.describe(eleccion="Cara o cruz", monto="Cuánto apostar (número, 'all' o 'mitad')")
async def slash_coinflip(interaction: discord.Interaction, eleccion: app_commands.Choice[str], monto: str):
    await interaction.response.send_message(**_eco_coinflip(interaction.guild, interaction.user, eleccion.value, monto))


@bot.tree.command(name="dice", description="Apuesta a un número del dado (paga x5)")
@app_commands.guild_only()
@app_commands.choices(numero=[
    app_commands.Choice(name="1", value="1"),
    app_commands.Choice(name="2", value="2"),
    app_commands.Choice(name="3", value="3"),
    app_commands.Choice(name="4", value="4"),
    app_commands.Choice(name="5", value="5"),
    app_commands.Choice(name="6", value="6"),
])
@app_commands.describe(numero="Número al que apuestas", monto="Cuánto apostar (número, 'all' o 'mitad')")
async def slash_dice(interaction: discord.Interaction, numero: app_commands.Choice[str], monto: str):
    await interaction.response.send_message(**_eco_dice(interaction.guild, interaction.user, int(numero.value), monto))


@bot.tree.command(name="highlow", description="Adivina si la carta es mayor o menor (x1.9)")
@app_commands.guild_only()
@app_commands.describe(monto="Cuánto apostar (número, 'all' o 'mitad')")
async def slash_highlow(interaction: discord.Interaction, monto: str):
    res = _eco_highlow_start(interaction.guild, interaction.user, monto)
    if res[0] == "msg":
        return await interaction.response.send_message(**res[1])
    embed, view = res[1]
    await interaction.response.send_message(embed=embed, view=view)
    view.msg = await interaction.original_response()


@bot.tree.command(name="roulette", description="Apuesta en la ruleta (rojo/negro x2, verde x14)")
@app_commands.guild_only()
@app_commands.choices(apuesta=[
    app_commands.Choice(name="Rojo", value="rojo"),
    app_commands.Choice(name="Negro", value="negro"),
    app_commands.Choice(name="Verde", value="verde"),
])
@app_commands.describe(apuesta="Color al que apuestas", monto="Cuánto apostar (número, 'all' o 'mitad')")
async def slash_roulette(interaction: discord.Interaction, apuesta: app_commands.Choice[str], monto: str):
    await interaction.response.send_message(**_eco_roulette(interaction.guild, interaction.user, apuesta.value, monto))


@bot.tree.command(name="blackjack", description="Juega al Blackjack contra el crupier (x2, natural x2.5)")
@app_commands.guild_only()
@app_commands.describe(monto="Cuánto apostar (número, 'all' o 'mitad')")
async def slash_blackjack(interaction: discord.Interaction, monto: str):
    res = _eco_blackjack_start(interaction.guild, interaction.user, monto)
    if res[0] == "msg":
        return await interaction.response.send_message(**res[1])
    embed, view = res[1]
    await interaction.response.send_message(embed=embed, view=view)
    view.msg = await interaction.original_response()


@bot.tree.command(name="baltop", description="Muestra el top de usuarios más ricos")
@app_commands.guild_only()
async def slash_baltop(interaction: discord.Interaction):
    await interaction.response.send_message(**_eco_baltop(interaction.guild))


@bot.tree.command(name="add-money", description="Añade dinero a un usuario (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(miembro="Usuario", monto="Cantidad a añadir")
async def slash_add_money(interaction: discord.Interaction, miembro: discord.Member, monto: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    await interaction.response.send_message(**_eco_admin_money(interaction.guild, "add", miembro, monto))


@bot.tree.command(name="remove-money", description="Quita dinero a un usuario (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(miembro="Usuario", monto="Cantidad a quitar")
async def slash_remove_money(interaction: discord.Interaction, miembro: discord.Member, monto: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    await interaction.response.send_message(**_eco_admin_money(interaction.guild, "remove", miembro, monto))


@bot.tree.command(name="set-money", description="Fija el efectivo de un usuario (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(miembro="Usuario", monto="Cantidad final")
async def slash_set_money(interaction: discord.Interaction, miembro: discord.Member, monto: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    await interaction.response.send_message(**_eco_admin_money(interaction.guild, "set", miembro, monto))


@bot.tree.command(name="set-currency", description="Cambia el símbolo de la moneda (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(simbolo="Nuevo símbolo (ej: €, 💎, puntos)")
async def slash_set_currency(interaction: discord.Interaction, simbolo: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    await interaction.response.send_message(**_eco_set_currency(interaction.guild, simbolo))


@bot.tree.command(name="set-start-balance", description="Fija el balance inicial para usuarios nuevos (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(monto="Balance inicial")
async def slash_set_start_balance(interaction: discord.Interaction, monto: int):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    await interaction.response.send_message(**_eco_set_start_balance(interaction.guild, str(monto)))


@bot.tree.command(name="economy-config", description="Muestra la configuración de economía (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def slash_economy_config(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    await interaction.response.send_message(**_eco_config_view(interaction.guild))


@bot.tree.command(name="reset-economy", description="Resetea TODA la economía del servidor (admin)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(confirmar="Marca True para confirmar el reseteo")
async def slash_reset_economy(interaction: discord.Interaction, confirmar: bool = False):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    if not confirmar:
        return await interaction.response.send_message("⚠️ Esto BORRARÁ todo el dinero de todos los usuarios. Marca `confirmar: True` para confirmar.")
    await interaction.response.send_message(**_eco_reset(interaction.guild))


# ============================================================
#  DASHBOARD WEB (aiohttp, corre junto al bot en el mismo loop)
#  Config en dashboard.json: enabled, host, port, token
# ============================================================

def _dashboard_html_path():
    """Ruta de dashboard.html junto al script (igual que token.txt)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DASHBOARD_HTML_PATH)


def _antiraid_public(cfg: dict):
    """Serializa la config antiraid para la API del dashboard."""
    stats = cfg.get("stats", {})
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "action": str(cfg.get("action", "kick")),
        "threshold": int(cfg.get("threshold", 5)),
        "seconds": int(cfg.get("seconds", 10)),
        "punish_new": bool(cfg.get("punish_new", True)),
        "min_age": int(cfg.get("min_age", 0)),
        "active": bool(cfg.get("active", False)),
        "manual": bool(cfg.get("manual", False)),
        "stats": {
            "raids": int(stats.get("raids", 0)),
            "punished": int(stats.get("punished", 0)),
        },
    }


def _dash_buscar_guild(gid_texto: str):
    """Devuelve el Guild a partir del parámetro de ruta, o None."""
    if not gid_texto.isdigit():
        return None
    return bot.get_guild(int(gid_texto))


@dash_web.middleware
async def _dash_auth(request, handler):
    """Si hay token configurado en dashboard.json, exige ?token= o cabecera X-Dashboard-Token."""
    token = dashboard_config.get("token", "")
    if token:
        provisto = request.query.get("token", "") or request.headers.get("X-Dashboard-Token", "")
        if provisto != token:
            return dash_web.json_response({"error": "No autorizado: token incorrecto"}, status=401)
    return await handler(request)


async def _dash_index(request):
    """Sirve la página del dashboard (se lee de disco en cada petición: editable en caliente)."""
    try:
        with open(_dashboard_html_path(), "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return dash_web.Response(text="dashboard.html no se encontró junto a bot.py", status=500, content_type="text/plain")
    return dash_web.Response(text=html, content_type="text/html", charset="utf-8")


async def _dash_status(request):
    ping = bot.latency
    ping_ms = round(ping * 1000) if ping == ping else None  # latency puede ser NaN antes del primer latido
    conectado = bot.user is not None and not bot.is_closed()
    usuarios = sum(g.member_count if g.member_count is not None else len(g.members) for g in bot.guilds)
    # Info del owner del bot (si está configurado en dashboard.json).
    owner = None
    owner_id = dashboard_config.get("owner_id", "")
    if owner_id.isdigit():
        usuario_owner = bot.get_user(int(owner_id))
        if usuario_owner is None:
            try:
                usuario_owner = await bot.fetch_user(int(owner_id))
            except (discord.NotFound, discord.HTTPException):
                usuario_owner = None
        if usuario_owner is not None:
            owner = {"id": str(usuario_owner.id), "nombre": str(usuario_owner), "avatar": str(usuario_owner.display_avatar.url)}
    return dash_web.json_response({
        "estado": "online" if conectado else "offline",
        "servidores": len(bot.guilds),
        "usuarios": usuarios,
        "ping_ms": ping_ms,
        "uptime_seg": int(time.time() - _INICIO_BOT),
        "owner": owner,
        "bot": {
            "nombre": str(bot.user) if bot.user else "WAVEBOT",
            "id": str(bot.user.id) if bot.user else None,
            "avatar": str(bot.user.display_avatar.url) if bot.user else None,
        },
    })


async def _dash_guilds(request):
    datos = []
    for g in sorted(bot.guilds, key=lambda x: -(x.member_count if x.member_count is not None else len(x.members))):
        datos.append({
            "id": str(g.id),  # como texto: los snowflakes superan la precisión de JS (53 bits)
            "nombre": g.name,
            "icono": str(g.icon.url) if g.icon else None,
            "miembros": g.member_count if g.member_count is not None else len(g.members),
        })
    return dash_web.json_response(datos)


async def _dash_guild(request):
    guild = _dash_buscar_guild(request.match_info["gid"])
    if guild is None:
        return dash_web.json_response({"error": "Servidor no encontrado"}, status=404)
    gid = str(guild.id)

    def nombres_canales(ids):
        nombres = []
        for cid in ids:
            canal = guild.get_channel(cid)
            if canal is not None:
                nombres.append("#" + canal.name)
        return nombres

    honeypots_nombres = []
    for cid in honeypots_db.get(gid, {}):
        canal = guild.get_channel(int(cid))
        if canal is not None:
            honeypots_nombres.append("#" + canal.name)

    autor_cfg = autoroles_db.get(gid, {})
    autoroles_count = sum(len(autor_cfg.get(k, [])) for k in ("all", "human", "bot"))

    warns_listas = [v for v in warns_db.values() if isinstance(v, list)]
    warns_totales = sum(len(v) for v in warns_listas)
    usuarios_warns = sum(1 for v in warns_listas if v)

    sb = starboard_db.get(gid, {})
    xpc = xp_config_db.get(gid, {})
    ec_cfg = econ_config_db.get(gid, {})
    ec_users = economy_db.get(gid, {})
    dinero_total = sum(int(u.get("cash", 0)) + int(u.get("bank", 0)) for u in ec_users.values() if isinstance(u, dict))

    return dash_web.json_response({
        "id": str(guild.id),  # como texto: los snowflakes superan la precisión de JS (53 bits)
        "nombre": guild.name,
        "icono": str(guild.icon.url) if guild.icon else None,
        "miembros": guild.member_count if guild.member_count is not None else len(guild.members),
        "bots": sum(1 for m in guild.members if m.bot),
        "owner_id": str(guild.owner_id),
        "owner_nombre": str(guild.owner) if guild.owner else None,
        "creado_texto": guild.created_at.strftime("%d/%m/%Y"),
        "prefijos": _get_prefixes_sync(guild.id),
        "antiraid": _antiraid_public(_antiraid_cfg(gid)),
        "moderacion": {
            "warns_totales": warns_totales,
            "usuarios_con_warns": usuarios_warns,
            "canales_logs": nombres_canales(logs_channels),
            "linkban": nombres_canales(linkban_canal),
            "honeypots": honeypots_nombres,
            "autoroles": autoroles_count,
        },
        "starboard": {
            "enabled": bool(sb.get("enabled", False)),
            "threshold": int(sb.get("threshold", 5)),
        },
        "xp": {
            "enabled": bool(xpc.get("enabled", False)),
            "xp_min": int(xpc.get("xp_min", 0) or 0),
            "xp_max": int(xpc.get("xp_max", 0) or 0),
            "cooldown": int(xpc.get("cooldown", 0) or 0),
            "usuarios": len(xp_db.get(gid, {})),
        },
        "economia": {
            "moneda": ec_cfg.get("currency", "$"),
            "usuarios": len(ec_users),
            "dinero_total": dinero_total,
            "items_tienda": len(shop_db.get(gid, {})),
        },
    })


async def _dash_antiraid_set(request):
    """POST /api/guild/<id>/antiraid — misma validación que el comando .antiraid."""
    guild = _dash_buscar_guild(request.match_info["gid"])
    if guild is None:
        return dash_web.json_response({"error": "Servidor no encontrado"}, status=404)
    try:
        data = await request.json()
    except Exception:
        return dash_web.json_response({"error": "JSON inválido"}, status=400)
    if not isinstance(data, dict):
        return dash_web.json_response({"error": "El cuerpo debe ser un objeto JSON"}, status=400)

    gid = str(guild.id)
    cfg = _antiraid_cfg(gid)
    cambios = []

    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            return dash_web.json_response({"error": "enabled debe ser true/false"}, status=400)
        cfg["enabled"] = data["enabled"]
        cambios.append("sistema " + ("activado" if data["enabled"] else "desactivado"))
        if not data["enabled"]:
            cfg["active"] = False
            cfg["activated_at"] = None
            cfg["manual"] = False

    if "threshold" in data:
        v = data["threshold"]
        if isinstance(v, bool) or not isinstance(v, int) or not (2 <= v <= 100):
            return dash_web.json_response({"error": "El umbral debe estar entre 2 y 100 joins"}, status=400)
        cfg["threshold"] = v
        cambios.append(f"umbral {v} joins")
    if "seconds" in data:
        v = data["seconds"]
        if isinstance(v, bool) or not isinstance(v, int) or not (3 <= v <= 3600):
            return dash_web.json_response({"error": "La ventana debe estar entre 3 y 3600 segundos"}, status=400)
        cfg["seconds"] = v
        cambios.append(f"ventana {v}s")
    if "action" in data:
        v = data["action"]
        if v not in ("ban", "kick", "mute"):
            return dash_web.json_response({"error": "La acción debe ser ban, kick o mute"}, status=400)
        cfg["action"] = v
        cambios.append(f"acción {v}")
    if "punish_new" in data:
        if not isinstance(data["punish_new"], bool):
            return dash_web.json_response({"error": "punish_new debe ser true/false"}, status=400)
        cfg["punish_new"] = data["punish_new"]
        cambios.append("castigar entradas: " + ("sí" if data["punish_new"] else "no"))
    if "min_age" in data:
        v = data["min_age"]
        if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= 43800):
            return dash_web.json_response({"error": "La edad mínima debe estar entre 0 y 43800 minutos"}, status=400)
        cfg["min_age"] = v
        cambios.append(f"edad mínima {v} min")

    guardar_antiraid()

    if cambios:
        embed = discord.Embed(
            title="⚙️ Antiraid actualizado desde el dashboard",
            description=" • ".join(cambios),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        await enviar_logs(guild, embed)

    return dash_web.json_response({"ok": True, "antiraid": _antiraid_public(cfg)})


async def _dash_raidmode(request):
    """POST /api/guild/<id>/raidmode — misma lógica que .antiraid raidmode."""
    guild = _dash_buscar_guild(request.match_info["gid"])
    if guild is None:
        return dash_web.json_response({"error": "Servidor no encontrado"}, status=404)
    try:
        data = await request.json()
    except Exception:
        return dash_web.json_response({"error": "JSON inválido"}, status=400)
    estado = data.get("estado")
    if estado not in ("on", "off"):
        return dash_web.json_response({"error": "estado debe ser on u off"}, status=400)

    gid = str(guild.id)
    cfg = _antiraid_cfg(gid)

    if estado == "on":
        if not cfg.get("enabled"):
            return dash_web.json_response({"error": "El antiraid está desactivado. Actívalo primero."}, status=400)
        cfg["active"] = True
        cfg["activated_at"] = time.time()
        cfg["manual"] = True
        guardar_antiraid()
        embed = discord.Embed(
            title="🚨 Modo raid activado (dashboard)",
            description="Todos los que entren ahora serán castigados hasta que se desactive.",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Se mantiene activo hasta apagarlo (dashboard o .antiraid raidmode off).")
        await enviar_logs(guild, embed)
        return dash_web.json_response({"ok": True, "antiraid": _antiraid_public(cfg)})

    cfg["active"] = False
    cfg["activated_at"] = None
    cfg["manual"] = False
    guardar_antiraid()
    embed = discord.Embed(title="✅ Modo raid desactivado (dashboard)", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    await enviar_logs(guild, embed)
    return dash_web.json_response({"ok": True, "antiraid": _antiraid_public(cfg)})


async def _dash_leave(request):
    """POST /api/guild/<id>/leave — el bot abandona ese servidor (solo owner)."""
    owner_id = str(dashboard_config.get("owner_id", ""))
    if not owner_id:
        return dash_web.json_response({"error": "No hay owner configurado (owner_id en dashboard.json)"}, status=403)
    guild = _dash_buscar_guild(request.match_info["gid"])
    if guild is None:
        return dash_web.json_response({"error": "Servidor no encontrado"}, status=404)
    nombre, gid = guild.name, guild.id
    try:
        await bot.leave_guild(guild)
    except (discord.Forbidden, discord.HTTPException) as e:
        return dash_web.json_response({"error": f"No pude salir del servidor: {e}"}, status=500)
    print(f"Dashboard: bot expulsado del servidor {nombre} (ID {gid})")
    return dash_web.json_response({"ok": True, "salido": nombre})


async def _dash_sync(request):
    """POST /api/sync — sincroniza los slash commands (solo owner)."""
    owner_id = str(dashboard_config.get("owner_id", ""))
    if not owner_id:
        return dash_web.json_response({"error": "No hay owner configurado (owner_id en dashboard.json)"}, status=403)
    try:
        sincronizados = await bot.tree.sync()
    except Exception as e:
        return dash_web.json_response({"error": f"Error sincronizando: {e}"}, status=500)
    return dash_web.json_response({"ok": True, "sincronizados": len(sincronizados)})


async def _iniciar_dashboard():
    """Arranca el servidor aiohttp del dashboard en el mismo event loop del bot."""
    app = dash_web.Application(middlewares=[_dash_auth])
    app.router.add_get("/", _dash_index)
    app.router.add_get("/api/status", _dash_status)
    app.router.add_get("/api/guilds", _dash_guilds)
    app.router.add_get("/api/guild/{gid}", _dash_guild)
    app.router.add_post("/api/guild/{gid}/antiraid", _dash_antiraid_set)
    app.router.add_post("/api/guild/{gid}/raidmode", _dash_raidmode)
    app.router.add_post("/api/guild/{gid}/leave", _dash_leave)
    app.router.add_post("/api/sync", _dash_sync)
    runner = dash_web.AppRunner(app)
    await runner.setup()
    site = dash_web.TCPSite(runner, dashboard_config["host"], dashboard_config["port"])
    await site.start()
    host = dashboard_config["host"]
    if host in ("0.0.0.0", "::"):
        host = "localhost"
    print(f"🚀 Dashboard disponible en: http://{host}:{dashboard_config['port']}")


@bot.command(name="dashboard")
@commands.has_permissions(manage_guild=True)
async def dashboard(ctx):
    """Muestra la URL del panel web del bot. Uso: .dashboard"""
    if not dashboard_config.get("enabled"):
        return await ctx.send("🔴 El dashboard está desactivado. Edita `dashboard.json` (`enabled`) y reinicia el bot.")
    host = dashboard_config["host"]
    if host in ("0.0.0.0", "::"):
        host = "localhost"
    url = f"http://{host}:{dashboard_config['port']}"
    embed = discord.Embed(
        title="📊 Panel web del bot",
        description=f"Controla el bot desde el navegador:\n**{url}**",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Qué puedes hacer", value="Ver estado en vivo, servidores, moderación, economía y configurar el **antiraid**.", inline=False)
    embed.set_footer(text="Si configuraste un token en dashboard.json, añade ?token=TUTOKEN a la URL.")
    await ctx.send(embed=embed)


@dashboard.error
async def dashboard_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Necesitas el permiso Manage Server.")


@bot.tree.command(name="dashboard", description="Muestra la URL del panel web del bot")
@app_commands.default_permissions(manage_guild=True)
async def slash_dashboard(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("❌ Necesitas el permiso Manage Server.", ephemeral=True)
    if not dashboard_config.get("enabled"):
        return await interaction.response.send_message("🔴 El dashboard está desactivado. Edita `dashboard.json` (`enabled`) y reinicia el bot.", ephemeral=True)
    host = dashboard_config["host"]
    if host in ("0.0.0.0", "::"):
        host = "localhost"
    url = f"http://{host}:{dashboard_config['port']}"
    embed = discord.Embed(
        title="📊 Panel web del bot",
        description=f"Controla el bot desde el navegador:\n**{url}**",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Qué puedes hacer", value="Ver estado en vivo, servidores, moderación, economía y configurar el **antiraid**.", inline=False)
    embed.set_footer(text="Si configuraste un token en dashboard.json, añade ?token=TUTOKEN a la URL.")
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    import os
    # Evitar UnicodeEncodeError en consolas de Windows (cp1252) al imprimir emojis.
    for _flujo in (sys.stdout, sys.stderr):
        if _flujo is not None and hasattr(_flujo, "reconfigure"):
            try:
                _flujo.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass
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
