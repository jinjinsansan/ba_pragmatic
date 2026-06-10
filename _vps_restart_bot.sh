#!/bin/bash
# Restart dual_line_pragmatic_bot safely (avoid pkill self-match by running from a file)
pkill -9 -f 'python dual_line_pragmatic_bot.py'
pkill -9 -f 'camoufox_profile_dual_line'
sleep 3
pkill -9 -f 'camoufox_profile_dual_line' 2>/dev/null
echo "KILLED; wrapper will respawn in ~20s"
