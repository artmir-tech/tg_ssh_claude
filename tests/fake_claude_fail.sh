#!/bin/sh
# Simulates a crashing Claude CLI for the error-recovery test.
echo "simulated worker crash: segmentation fault (core dumped)" >&2
exit 1
