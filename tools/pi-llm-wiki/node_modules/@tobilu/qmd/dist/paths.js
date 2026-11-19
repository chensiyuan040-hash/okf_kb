import { homedir as osHomedir } from "node:os";
export function qmdHomedir() {
    return process.env.HOME || process.env.USERPROFILE || osHomedir() || "/tmp";
}
