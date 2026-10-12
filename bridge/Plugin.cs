using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using Dalamud.Game.Command;
using Dalamud.IoC;
using Dalamud.Plugin;
using Dalamud.Plugin.Services;
using Lumina.Excel.Sheets;

namespace WorkshopBridge;

public sealed class Plugin : IDalamudPlugin
{
    [PluginService] internal static ICommandManager Commands { get; private set; } = null!;
    [PluginService] internal static IPlayerState Player { get; private set; } = null!;
    [PluginService] internal static IDataManager Data { get; private set; } = null!;
    [PluginService] internal static IFramework Framework { get; private set; } = null!;
    [PluginService] internal static IChatGui Chat { get; private set; } = null!;
    [PluginService] internal static IPluginLog Log { get; private set; } = null!;

    private bool automatic;
    private DateTime nextWrite = DateTime.MinValue;
    private static readonly Dictionary<uint, string> Jobs = new()
    {
        [8] = "Carpenter", [9] = "Blacksmith", [10] = "Armorer", [11] = "Goldsmith",
        [12] = "Leatherworker", [13] = "Weaver", [14] = "Alchemist", [15] = "Culinarian",
        [16] = "Miner", [17] = "Botanist", [18] = "Fisher",
    };

    public Plugin()
    {
        Commands.AddHandler("/workshop", new CommandInfo(Command)
        {
            HelpMessage = "Export levels/EXP locally. /workshop auto toggles 5-second exports.",
        });
        Framework.Update += Update;
    }

    private void Command(string command, string args)
    {
        if (args.Trim().Equals("auto", StringComparison.OrdinalIgnoreCase))
        {
            automatic = !automatic;
            Chat.Print("Workshop: automatic export " + (automatic ? "enabled" : "disabled") + ".");
        }
        Export(true);
    }

    private void Update(IFramework framework)
    {
        if (!automatic || DateTime.UtcNow < nextWrite || !Player.IsLoaded)
            return;
        nextWrite = DateTime.UtcNow.AddSeconds(5);
        Export(false);
    }

    private void Export(bool report)
    {
        if (!Player.IsLoaded)
        {
            if (report) Chat.Print("Workshop: log in to your character first.");
            return;
        }
        try
        {
            var sheet = Data.GetExcelSheet<ClassJob>();
            var jobs = new Dictionary<string, object>();
            foreach (var (id, name) in Jobs)
            {
                var job = sheet.GetRow(id);
                jobs[name] = new { level = (int)Player.GetClassJobLevel(job), xp = Math.Max(0, Player.GetClassJobExperience(job)) };
            }
            var snapshot = new
            {
                schema = 1,
                source = "WorkshopBridge",
                updated_at = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                home_world = Player.HomeWorld.Value.Name.ToString(),
                current_world = Player.CurrentWorld.Value.Name.ToString(),
                current_job = Jobs.GetValueOrDefault(Player.ClassJob.RowId, ""),
                jobs,
            };
            var folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FFXIVWorkshop");
            Directory.CreateDirectory(folder);
            var path = Path.Combine(folder, "character.json");
            File.WriteAllText(path + ".tmp", JsonSerializer.Serialize(snapshot));
            File.Move(path + ".tmp", path, true);
            if (report) Chat.Print("Workshop: snapshot exported to " + path);
        }
        catch (Exception ex)
        {
            Log.Error(ex, "Workshop snapshot export failed.");
            if (report) Chat.Print("Workshop: export failed. See /xllog.");
            automatic = false;
        }
    }

    public void Dispose()
    {
        Framework.Update -= Update;
        Commands.RemoveHandler("/workshop");
    }
}
