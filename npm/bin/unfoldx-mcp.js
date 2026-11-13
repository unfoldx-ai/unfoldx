#!/usr/bin/env node
import { main } from "../src/install.js";

process.exitCode = main(process.argv.slice(2));
