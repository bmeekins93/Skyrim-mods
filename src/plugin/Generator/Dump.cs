using System.Globalization;
using System.Text;
using Mutagen.Bethesda.Plugins;
using Mutagen.Bethesda.Plugins.Records;
using Mutagen.Bethesda.Skyrim;

namespace CardboardFaceMask.Generator;

/// <summary>
/// Human-readable listing of every field of every record in the plugin. Records are listed in
/// FormID order (not file order), so two plugins that differ only in record order dump identically.
/// </summary>
public static class Dump
{
    public static string Render(ISkyrimModGetter mod)
    {
        var sb = new StringBuilder();
        var masters = mod.ModHeader.MasterReferences.Select(m => m.Master).ToList();
        var own = mod.EnumerateMajorRecords().ToDictionary(r => r.FormKey, r => r.EditorID ?? "?");
        string F(FormKey key) => Describe(key, mod.ModKey, masters, own);
        string Link<T>(IFormLinkNullableGetter<T> link) where T : class, IMajorRecordGetter
            => link.IsNull ? "(none)" : F(link.FormKey);

        var h = mod.ModHeader;
        sb.AppendLine($"Plugin {mod.ModKey}");
        sb.AppendLine($"  TES4 flags        0x{(uint)h.Flags:X8} ({h.Flags})");
        sb.AppendLine($"  HEDR version      {h.Stats.Version.ToString("0.00", CultureInfo.InvariantCulture)}");
        sb.AppendLine($"  HEDR num records  {h.Stats.NumRecords}");
        sb.AppendLine($"  HEDR next ID      0x{h.Stats.NextFormID:X}");
        sb.AppendLine($"  Form version      {h.FormVersion}");
        sb.AppendLine($"  Author / Desc     '{h.Author}' / '{h.Description}'");
        sb.AppendLine($"  Masters           {string.Join(", ", masters)}");

        foreach (var armor in mod.Armors.OrderBy(r => r.FormKey.ID))
        {
            sb.AppendLine($"ARMO {F(armor.FormKey)}");
            sb.AppendLine($"  EDID              {armor.EditorID}");
            sb.AppendLine($"  Record header     flags 0x{(uint)armor.MajorFlags:X} ({armor.MajorFlags}); {Header(armor)}");
            sb.AppendLine($"  VMAD              {(armor.VirtualMachineAdapter is null ? "(none)" : "present")}");
            var b = armor.ObjectBounds;
            sb.AppendLine($"  OBND              {b.First} .. {b.Second}");
            sb.AppendLine($"  FULL              '{armor.Name?.String}'");
            sb.AppendLine($"  EITM / EAMT       {Link(armor.ObjectEffect)} / {armor.EnchantmentAmount?.ToString() ?? "(none)"}");
            sb.AppendLine($"  MOD2 world (M)    {ModelText(armor.WorldModel?.Male?.Model)}; icons {IconText(armor.WorldModel?.Male?.Icons)}");
            sb.AppendLine($"  MOD4 world (F)    {ModelText(armor.WorldModel?.Female?.Model)}; icons {IconText(armor.WorldModel?.Female?.Icons)}");
            sb.AppendLine($"  BOD2              {Body(armor.BodyTemplate)}");
            sb.AppendLine($"  DEST              {(armor.Destructible is null ? "(none)" : "present")}");
            sb.AppendLine($"  YNAM pick up      {Link(armor.PickUpSound)}");
            sb.AppendLine($"  ZNAM put down     {Link(armor.PutDownSound)}");
            sb.AppendLine($"  BMCT ragdoll      {armor.RagdollConstraintTemplate ?? "(none)"}");
            sb.AppendLine($"  ETYP equip type   {Link(armor.EquipmentType)}");
            sb.AppendLine($"  BIDS / BAMT       {Link(armor.BashImpactDataSet)} / {Link(armor.AlternateBlockMaterial)}");
            sb.AppendLine($"  RNAM race         {Link(armor.Race)}");
            sb.AppendLine($"  KWDA keywords     {string.Join(", ", (armor.Keywords ?? Array.Empty<IFormLinkGetter<IKeywordGetter>>()).Select(k => F(k.FormKey)))}");
            sb.AppendLine($"  DESC              '{armor.Description?.String}'");
            sb.AppendLine($"  MODL armature     {string.Join(" -> ", armor.Armature.Select(a => F(a.FormKey)))}");
            sb.AppendLine($"  DATA value/weight {armor.Value} / {armor.Weight.ToString(CultureInfo.InvariantCulture)}");
            sb.AppendLine($"  DNAM armor rating {armor.ArmorRating.ToString(CultureInfo.InvariantCulture)}");
            sb.AppendLine($"  TNAM template     {Link(armor.TemplateArmor)}");
        }

        foreach (var aa in mod.ArmorAddons.OrderBy(r => r.FormKey.ID))
        {
            sb.AppendLine($"ARMA {F(aa.FormKey)}");
            sb.AppendLine($"  EDID              {aa.EditorID}");
            sb.AppendLine($"  Record header     flags 0x{(uint)aa.MajorRecordFlagsRaw:X}; {Header(aa)}");
            sb.AppendLine($"  BOD2              {Body(aa.BodyTemplate)}");
            sb.AppendLine($"  RNAM race         {Link(aa.Race)}");
            sb.AppendLine($"  DNAM priority M/F {aa.Priority.Male} / {aa.Priority.Female}");
            sb.AppendLine($"  DNAM weight slider M/F {aa.WeightSliderEnabled.Male} / {aa.WeightSliderEnabled.Female}");
            sb.AppendLine($"  DNAM unknown      {aa.Unknown} / {aa.Unknown2}");
            sb.AppendLine($"  DNAM detection snd {aa.DetectionSoundValue}");
            sb.AppendLine($"  DNAM weapon adjust {aa.WeaponAdjust.ToString(CultureInfo.InvariantCulture)}");
            sb.AppendLine($"  MOD2 world M      {ModelText(aa.WorldModel?.Male)}");
            sb.AppendLine($"  MOD3 world F      {ModelText(aa.WorldModel?.Female)}");
            sb.AppendLine($"  MOD4 1st person M {ModelText(aa.FirstPersonModel?.Male)}");
            sb.AppendLine($"  MOD5 1st person F {ModelText(aa.FirstPersonModel?.Female)}");
            sb.AppendLine($"  NAM0/NAM1 skin    {Link(aa.SkinTexture?.Male ?? FormLinkNullableGetter<ITextureSetGetter>.Null)} / {Link(aa.SkinTexture?.Female ?? FormLinkNullableGetter<ITextureSetGetter>.Null)}");
            sb.AppendLine($"  NAM2/NAM3 swap    {Link(aa.TextureSwapList?.Male ?? FormLinkNullableGetter<IFormListGetter>.Null)} / {Link(aa.TextureSwapList?.Female ?? FormLinkNullableGetter<IFormListGetter>.Null)}");
            sb.AppendLine($"  MODL races ({aa.AdditionalRaces.Count,2})  {string.Join(", ", aa.AdditionalRaces.Select(r => F(r.FormKey)))}");
            sb.AppendLine($"  SNDD footstep     {Link(aa.FootstepSound)}");
            sb.AppendLine($"  ONAM art object   {Link(aa.ArtObject)}");
        }
        return sb.ToString();
    }

    private static string Header(ISkyrimMajorRecordGetter r) =>
        $"form version {r.FormVersion}, version control 0x{r.VersionControl:X}, version2 {r.Version2}";

    /// <summary>Model path plus its optional MODT texture-hash data and alternate texture sets.</summary>
    private static string ModelText(IModelGetter? m) => m is null
        ? "(none)"
        : $"{m.File.GivenPath} (texture data: {(m.Data is null ? "none" : $"{m.Data.Value.Length} bytes")}, " +
          $"alternate textures: {m.AlternateTextures?.Count ?? 0})";

    private static string IconText(IIconsGetter? icons) => icons is null
        ? "(none)"
        : $"ICON '{icons.LargeIconFilename.GivenPath}', MICO '{icons.SmallIconFilename?.GivenPath}'";

    private static string Body(IBodyTemplateGetter? b)
    {
        if (b is null) return "(none)";
        uint bits = (uint)b.FirstPersonFlags;
        var slots = Enumerable.Range(0, 32).Where(i => (bits & (1u << i)) != 0).Select(i => (30 + i).ToString(CultureInfo.InvariantCulture));
        return $"flags 0x{bits:X8} (slots {string.Join(",", slots)}), armor type {b.ArmorType}, BODT flags {b.Flags}";
    }

    /// <summary>FormKey with EditorID and the FormID exactly as stored in this file (master index prefix).</summary>
    private static string Describe(FormKey key, ModKey self, IReadOnlyList<ModKey> masters, IReadOnlyDictionary<FormKey, string> own)
    {
        int index = key.ModKey == self ? masters.Count : masters.ToList().IndexOf(key.ModKey);
        string stored = index < 0 ? "????????" : $"{((uint)index << 24) | key.ID:X8}";
        string name = key.ModKey == self ? own.GetValueOrDefault(key, "?") : Vanilla.NameOf(key);
        return $"[{stored}] {name} ({key})";
    }
}
