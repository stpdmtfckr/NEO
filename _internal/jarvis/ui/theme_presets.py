# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Shared UI/overlay theme presets.

Kept in a lightweight module so Settings and Telegram Remote use exactly
the same preset inventory without importing the full Qt Settings dialog.
"""

THEME_PRESETS: dict[str, dict[str, dict[str, str]]] = {
    "NEO Cyan": {
        "window": {"accent":"#57D7FF","window_color":"#0C1017","panel_color":"#111722","border_color":"#57D7FF","text_color":"#EAF2FF","muted_text_color":"#8F9DB2","element_color":"#192333","element_hover_color":"#243247","chat_color":"#111722","chat_user_color":"#23465A","chat_assistant_color":"#182232"},
        "robot": {"robot_body_color":"#313C4F","robot_shell_color":"#DDE7F4","robot_screen_color":"#101721","robot_eye_color":"#57D7FF","robot_trim_color":"#718198","robot_glow_color":"#57D7FF"},
    },
    "NEO Orange": {
        "window": {"accent":"#FF9838","window_color":"#120D09","panel_color":"#1A120D","border_color":"#FF9838","text_color":"#FFF3E7","muted_text_color":"#B89A83","element_color":"#281A12","element_hover_color":"#3B2618","chat_color":"#17100B","chat_user_color":"#60361C","chat_assistant_color":"#241811"},
        "robot": {"robot_body_color":"#4B392E","robot_shell_color":"#F2E6DA","robot_screen_color":"#18100B","robot_eye_color":"#FF9838","robot_trim_color":"#9B7358","robot_glow_color":"#FF9838"},
    },
    "NEO Blue": {
        "window": {"accent":"#4B8DFF","window_color":"#090E18","panel_color":"#101827","border_color":"#4B8DFF","text_color":"#EAF2FF","muted_text_color":"#899BB8","element_color":"#17243A","element_hover_color":"#223653","chat_color":"#0E1624","chat_user_color":"#244674","chat_assistant_color":"#16243A"},
        "robot": {"robot_body_color":"#303D56","robot_shell_color":"#DFE8F7","robot_screen_color":"#0C1422","robot_eye_color":"#4B8DFF","robot_trim_color":"#7188AE","robot_glow_color":"#4B8DFF"},
    },
    "NEO Purple": {
        "window": {"accent":"#B06CFF","window_color":"#100A17","panel_color":"#181021","border_color":"#B06CFF","text_color":"#F5EDFF","muted_text_color":"#A390B8","element_color":"#251735","element_hover_color":"#38224F","chat_color":"#150D1E","chat_user_color":"#4B286C","chat_assistant_color":"#21152F"},
        "robot": {"robot_body_color":"#44344F","robot_shell_color":"#EEE4F7","robot_screen_color":"#150C1F","robot_eye_color":"#B06CFF","robot_trim_color":"#8A6C9D","robot_glow_color":"#B06CFF"},
    },
    "NEO Yellow": {
        "window": {"accent":"#FFD84A","window_color":"#121006","panel_color":"#1A1709","border_color":"#FFD84A","text_color":"#FFF9DC","muted_text_color":"#B7AB79","element_color":"#29240E","element_hover_color":"#3D3514","chat_color":"#171407","chat_user_color":"#5D5017","chat_assistant_color":"#24200D"},
        "robot": {"robot_body_color":"#4A4430","robot_shell_color":"#F2ECD8","robot_screen_color":"#181506","robot_eye_color":"#FFD84A","robot_trim_color":"#948760","robot_glow_color":"#FFD84A"},
    },
    "NEO Green": {
        "window": {"accent":"#45E58A","window_color":"#08120D","panel_color":"#0E1B14","border_color":"#45E58A","text_color":"#E9FFF3","muted_text_color":"#82AF97","element_color":"#14281D","element_hover_color":"#1D3A2A","chat_color":"#0B1710","chat_user_color":"#1E5A3A","chat_assistant_color":"#12251B"},
        "robot": {"robot_body_color":"#304A3B","robot_shell_color":"#DFEFE5","robot_screen_color":"#09170F","robot_eye_color":"#45E58A","robot_trim_color":"#6F957E","robot_glow_color":"#45E58A"},
    },
    "NEO Red": {
        "window": {"accent":"#FF5268","window_color":"#14090C","panel_color":"#1D0F13","border_color":"#FF5268","text_color":"#FFF0F2","muted_text_color":"#B68B92","element_color":"#2C151B","element_hover_color":"#421E27","chat_color":"#190C10","chat_user_color":"#672331","chat_assistant_color":"#28131A"},
        "robot": {"robot_body_color":"#50343A","robot_shell_color":"#F2E3E6","robot_screen_color":"#190A0F","robot_eye_color":"#FF5268","robot_trim_color":"#9B6C75","robot_glow_color":"#FF5268"},
    },
    "NEO Gray": {
        "window": {"accent":"#C7CDD6","window_color":"#0D0F12","panel_color":"#15181D","border_color":"#C7CDD6","text_color":"#F2F4F7","muted_text_color":"#98A0AB","element_color":"#1F232A","element_hover_color":"#2E343E","chat_color":"#111419","chat_user_color":"#3B434E","chat_assistant_color":"#1A1E25"},
        "robot": {"robot_body_color":"#30363F","robot_shell_color":"#ECEFF3","robot_screen_color":"#0E1116","robot_eye_color":"#F3F5F8","robot_trim_color":"#7E8794","robot_glow_color":"#C7CDD6"},
    },
    "NEO Dark": {
        "window": {"accent":"#F3F4F6","window_color":"#050607","panel_color":"#0B0D10","border_color":"#F3F4F6","text_color":"#F8F9FB","muted_text_color":"#A7ACB5","element_color":"#111419","element_hover_color":"#1C2027","chat_color":"#080A0D","chat_user_color":"#20242C","chat_assistant_color":"#11151B"},
        "robot": {"robot_body_color":"#14171C","robot_shell_color":"#F4F6F8","robot_screen_color":"#050608","robot_eye_color":"#FFFFFF","robot_trim_color":"#666D78","robot_glow_color":"#ECEFF4"},
    },
    "Full Dark": {
        "window": {"accent":"#FFFFFF","window_color":"#000000","panel_color":"#040506","border_color":"#FFFFFF","text_color":"#FFFFFF","muted_text_color":"#B4B7BD","element_color":"#0A0B0D","element_hover_color":"#16181B","chat_color":"#020304","chat_user_color":"#181A1E","chat_assistant_color":"#0A0C10"},
        "robot": {"robot_body_color":"#0A0B0D","robot_shell_color":"#FFFFFF","robot_screen_color":"#000000","robot_eye_color":"#FFFFFF","robot_trim_color":"#7C828B","robot_glow_color":"#FFFFFF"},
    },
    "NEO White": {
        "window": {"accent":"#3B4652","window_color":"#FBFCFD","panel_color":"#EFF2F5","border_color":"#FFFFFF","text_color":"#111419","muted_text_color":"#69727D","element_color":"#E4E9EE","element_hover_color":"#D5DBE2","chat_color":"#F7F9FB","chat_user_color":"#DCE3EA","chat_assistant_color":"#ECEFF3"},
        "robot": {"robot_body_color":"#2F3640","robot_shell_color":"#FFFFFF","robot_screen_color":"#111419","robot_eye_color":"#FFFFFF","robot_trim_color":"#8A93A0","robot_glow_color":"#DDE3EA"},
    },
    "Obsidian Red": {
        "window": {"accent":"#FF3B4D","window_color":"#08090C","panel_color":"#101217","border_color":"#FF3B4D","text_color":"#F7F7F8","muted_text_color":"#9B9DA5","element_color":"#171A20","element_hover_color":"#242832","chat_color":"#0D0F13","chat_user_color":"#4A1820","chat_assistant_color":"#171A20"},
        "robot": {"robot_body_color":"#17191E","robot_shell_color":"#F3F4F6","robot_screen_color":"#090A0D","robot_eye_color":"#FF3B4D","robot_trim_color":"#6E737D","robot_glow_color":"#FF263E"},
    },
    "Ember Orange": {
        "window": {"accent":"#FF8A2A","window_color":"#090A0C","panel_color":"#111317","border_color":"#FF8A2A","text_color":"#FFF8F0","muted_text_color":"#A99E93","element_color":"#1A1C20","element_hover_color":"#2A2520","chat_color":"#0E0F12","chat_user_color":"#56351E","chat_assistant_color":"#191B20"},
        "robot": {"robot_body_color":"#1A1C21","robot_shell_color":"#F7F4EF","robot_screen_color":"#0B0C0F","robot_eye_color":"#FF8A2A","robot_trim_color":"#7D7167","robot_glow_color":"#FF7A16"},
    },
    "Mono Tech": {
        "window": {"accent":"#E9EDF2","window_color":"#08090B","panel_color":"#111317","border_color":"#DDE2E8","text_color":"#F7F8FA","muted_text_color":"#969BA4","element_color":"#191C21","element_hover_color":"#2A2E35","chat_color":"#0F1115","chat_user_color":"#30353C","chat_assistant_color":"#181B20"},
        "robot": {"robot_body_color":"#202329","robot_shell_color":"#F5F6F7","robot_screen_color":"#090A0C","robot_eye_color":"#F4F6F8","robot_trim_color":"#7D838D","robot_glow_color":"#DDE2E8"},
    },
    "Arctic Red": {
        "window": {"accent":"#E82F43","window_color":"#F1F3F6","panel_color":"#FFFFFF","border_color":"#E82F43","text_color":"#12151A","muted_text_color":"#67707C","element_color":"#E5E8ED","element_hover_color":"#D7DBE2","chat_color":"#F8F9FB","chat_user_color":"#FFDDE2","chat_assistant_color":"#ECEFF3"},
        "robot": {"robot_body_color":"#2A2D33","robot_shell_color":"#FFFFFF","robot_screen_color":"#12151A","robot_eye_color":"#E82F43","robot_trim_color":"#747B85","robot_glow_color":"#FF5364"},
    },
    "Crimson White": {
        "window": {"accent":"#B70924","window_color":"#FAFAFB","panel_color":"#F0F1F3","border_color":"#B70924","text_color":"#101216","muted_text_color":"#666B74","element_color":"#E2E4E8","element_hover_color":"#D4D7DD","chat_color":"#F5F6F8","chat_user_color":"#F6CCD3","chat_assistant_color":"#E8EAEE"},
        "robot": {"robot_body_color":"#101216","robot_shell_color":"#F7F7F8","robot_screen_color":"#08090B","robot_eye_color":"#D31431","robot_trim_color":"#5D626B","robot_glow_color":"#E21C3A"},
    },
    "Carbon Red": {
        "window": {"accent":"#D71936","window_color":"#121316","panel_color":"#1A1C20","border_color":"#6F0E1D","text_color":"#F2F3F5","muted_text_color":"#8B909A","element_color":"#23262C","element_hover_color":"#30343C","chat_color":"#16181C","chat_user_color":"#4A1D25","chat_assistant_color":"#202329"},
        "robot": {"robot_body_color":"#090A0C","robot_shell_color":"#C8CDD4","robot_screen_color":"#050607","robot_eye_color":"#E81C3D","robot_trim_color":"#4D525B","robot_glow_color":"#C90F2D"},
    },
    "Void Orange": {
        "window": {"accent":"#FF6B00","window_color":"#050506","panel_color":"#0D0E10","border_color":"#B84E00","text_color":"#F5F5F5","muted_text_color":"#878B92","element_color":"#15171A","element_hover_color":"#272019","chat_color":"#090A0C","chat_user_color":"#49301D","chat_assistant_color":"#14161A"},
        "robot": {"robot_body_color":"#0C0D10","robot_shell_color":"#D9DDE3","robot_screen_color":"#030405","robot_eye_color":"#FF6B00","robot_trim_color":"#535862","robot_glow_color":"#FF6B00"},
    },
    "White Graphite": {
        "window": {"accent":"#343B45","window_color":"#FFFFFF","panel_color":"#F3F5F7","border_color":"#444C57","text_color":"#111419","muted_text_color":"#6C7480","element_color":"#E6E9ED","element_hover_color":"#D7DCE2","chat_color":"#F8F9FA","chat_user_color":"#DCE2E8","chat_assistant_color":"#EBEEF2"},
        "robot": {"robot_body_color":"#343A43","robot_shell_color":"#FFFFFF","robot_screen_color":"#14171B","robot_eye_color":"#E8EDF2","robot_trim_color":"#777F8B","robot_glow_color":"#AEB6C1"},
    },
    "Neon Violet": {
        "window": {"accent":"#A66CFF","window_color":"#0A0810","panel_color":"#13101C","border_color":"#9D5CFF","text_color":"#F5F0FF","muted_text_color":"#9D91AE","element_color":"#1E1929","element_hover_color":"#302540","chat_color":"#100D17","chat_user_color":"#3E285C","chat_assistant_color":"#1B1625"},
        "robot": {"robot_body_color":"#25202E","robot_shell_color":"#EEE7F8","robot_screen_color":"#0D0913","robot_eye_color":"#A66CFF","robot_trim_color":"#736781","robot_glow_color":"#9D5CFF"},
    },
}
