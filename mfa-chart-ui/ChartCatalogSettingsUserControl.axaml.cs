using Avalonia.Controls;
using MFAAvalonia.ViewModels.UsersControls.Settings;

namespace MFAAvalonia.Views.UserControls.Settings;

public partial class ChartCatalogSettingsUserControl : UserControl
{
    public ChartCatalogSettingsUserControl()
    {
        var model = MFAAvalonia.Helper.MaaPjskFirstLiveSync.Model;
        DataContext = model;
        InitializeComponent();
        Loaded += (_, _) => model.RefreshCommand.Execute(null);
    }
}
